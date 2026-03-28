"""Interactive arrow-key driven main menu for watchupdog."""

from __future__ import annotations

import os
import sys
import subprocess
import shutil
import threading
import queue as _queue
from pathlib import Path
from typing import Any, Callable
import argparse

# ── Privilege limits (populated once at run_menu startup) ──────────────────────
# Checked by _display_menu() to show UAC warnings and by the 'A' key handler.
_priv_limits: dict[str, Any] = {
    "is_admin": True,       # assume fine until proven otherwise
    "net_connections": True,
    "backup_writable": True,
}

# Full path to the launcher script (bat/sh) that started this process.
# Set by run_menu() from the --launcher argument; used by _relaunch_as_admin().
_launcher_path: str = ""

# Set to True after a repair runs so the menu shows a persistent restart notice.
_restart_pending: bool = False


def _relaunch_as_admin() -> bool:
    """
    Re-launch this process with UAC elevation (Windows only).

    Preferred path: re-run the original launcher script (whatever it is named,
    wherever it lives) via cmd.exe so the elevated session is identical to a
    normal launch — Python discovery, dep checks, and TOML reading all repeat.

    Fallback: if no launcher path was recorded, launch python.exe -m directly
    so at least module imports work correctly in the elevated process.

    Returns True if the elevated process was successfully started (caller
    should exit), or False if the user cancelled the UAC prompt or we are
    not on Windows.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    if _launcher_path and Path(_launcher_path).is_file():
        launcher = Path(_launcher_path).resolve()
        work_dir = str(launcher.parent)
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe",
                f'/c "{launcher}"',
                work_dir, 1,
            )
            return int(ret) > 32
        except Exception:
            pass  # fall through to python -m fallback

    # Fallback: launch python.exe -m directly (launcher not recorded or missing).
    extra = sys.argv[1:]
    work_dir_fb: str | None = None
    for i, a in enumerate(extra):
        if a == "--monitor-dir" and i + 1 < len(extra):
            work_dir_fb = extra[i + 1]
            break
    params = " ".join(f'"{a}"' for a in ["-m", "watchupdog.interactive_menu"] + extra)
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, work_dir_fb, 1,
        )
        return int(ret) > 32
    except Exception:
        return False

# ── Windows console helpers (no VT sequences needed) ───────────────────────────
if os.name == "nt":
    import ctypes as _ct
    import ctypes.wintypes as _wt

    class _COORD(_ct.Structure):
        _fields_ = [("X", _ct.c_short), ("Y", _ct.c_short)]

    class _CURSOR_INFO(_ct.Structure):
        _fields_ = [("dwSize", _ct.c_ulong), ("bVisible", _ct.c_bool)]

    _k32    = _ct.windll.kernel32
    _STDOUT = _k32.GetStdHandle(-11)

    def _cursor_visible(show: bool) -> None:
        ci = _CURSOR_INFO()
        if _k32.GetConsoleCursorInfo(_STDOUT, _ct.byref(ci)):
            ci.bVisible = show
            _k32.SetConsoleCursorInfo(_STDOUT, _ct.byref(ci))

    def _win_show_cursor() -> None:
        try:
            _cursor_visible(True)
        except Exception:
            pass

    def _win_hide_cursor() -> None:
        try:
            _cursor_visible(False)
        except Exception:
            pass

    def _win_cls() -> None:
        """Hide cursor and home — overwrites in place, no blank-frame flash.
        Use for altscreen redraws (picker, menu)."""
        _cursor_visible(False)
        try:
            sys.stdout.write("\x1b[?25l\x1b[H")
            sys.stdout.flush()
        except Exception:
            pass

    def _screen_wipe() -> None:
        """Hide cursor, erase the entire visible area, then home.
        Use whenever a clean slate is needed — after _exit_altscreen() before
        subprocess output, or inside altscreen before rendering content that
        is shorter than the previous render (prevents bleed-through)."""
        _cursor_visible(False)
        try:
            sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H")
            sys.stdout.flush()
        except Exception:
            pass

    def _enter_altscreen() -> None:
        """Switch to the alternate screen buffer (no scrollback) and hide cursor."""
        try:
            h = _k32.GetStdHandle(-11)
            mode = _ct.c_ulong(0)
            _k32.GetConsoleMode(h, _ct.byref(mode))
            _k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass
        try:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
        except Exception:
            pass

    def _exit_altscreen() -> None:
        """Return to the main screen buffer and restore cursor visibility."""
        try:
            sys.stdout.write("\x1b[?1049l\x1b[?25h")
            sys.stdout.flush()
        except Exception:
            pass

else:
    def _win_show_cursor() -> None:   # type: ignore[misc]
        sys.stdout.write("\x1b[?25h"); sys.stdout.flush()

    def _win_hide_cursor() -> None:   # type: ignore[misc]
        sys.stdout.write("\x1b[?25l"); sys.stdout.flush()

    def _win_cls() -> None:           # type: ignore[misc]
        sys.stdout.write("\x1b[?25l\x1b[H"); sys.stdout.flush()

    def _screen_wipe() -> None:       # type: ignore[misc]
        sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H"); sys.stdout.flush()

    def _enter_altscreen() -> None:   # type: ignore[misc]
        sys.stdout.write("\x1b[?1049h\x1b[?25l"); sys.stdout.flush()

    def _exit_altscreen() -> None:    # type: ignore[misc]
        sys.stdout.write("\x1b[?1049l\x1b[?25h"); sys.stdout.flush()

from rich.console import Console
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from watchupdog.settings_editor import _get_key, _read_line, _ESC


# ── Python version detection ───────────────────────────────────────────────────
# Queried once from the live /system_stats API (authoritative — works regardless
# of install layout), then cached for the whole session.  Falls back to
# venv-on-disk probing, then the Python running this script.

# Cache keyed by comfyui_path so each installation gets its own Python version.
# Switching installations automatically gets a fresh lookup.
_py_cache: dict[str, str] = {}

# ── ComfyUI connectivity probe (non-blocking, background thread) ───────────────
# Updated every 5 s in the background so the header dot never stalls the render.
_conn_state: dict[str, bool] = {}        # url → online
_conn_last:  dict[str, float] = {}       # url → monotonic timestamp of last probe


def _probe_connectivity(url: str) -> None:
    """Background thread: hit /system_stats and record the result."""
    import urllib.request as _ur
    try:
        _ur.urlopen(f"{url}/system_stats", timeout=2.0)
        _conn_state[url] = True
    except Exception:
        _conn_state[url] = False


def _refresh_connectivity(url: str) -> None:
    """Trigger a background probe if the cached result is older than 5 s."""
    import time
    now = time.monotonic()
    if now - _conn_last.get(url, 0.0) >= 5.0:
        _conn_last[url] = now
        threading.Thread(target=_probe_connectivity, args=(url,), daemon=True).start()


def _fetch_comfyui_python(url: str) -> str | None:
    """Ask the running ComfyUI instance what Python version it uses."""
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen(f"{url}/system_stats", timeout=3) as r:
            data = _json.loads(r.read())
        ver = (data.get("python_version")
               or data.get("system", {}).get("python_version"))
        if ver:
            return str(ver).split()[0]   # "3.10.11 (tags/…)" → "3.10.11"
    except Exception:
        pass
    return None


def _py_ver_exe(exe: str) -> str | None:
    """Return 'X.Y.Z' for a given python executable path."""
    try:
        r = subprocess.run(
            [exe, "-c", "import sys;print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=4,
        )
        v = r.stdout.strip()
        return v if v else None
    except Exception:
        return None


def _detect_python_info(comfyui_path: str, url: str = "") -> str:
    """
    Return the Python version string shown in the menu header.
    Priority:
      1. Live API  — /system_stats from the running ComfyUI (most accurate)
      2. On-disk   — venv / embedded Python inside the ComfyUI folder
      3. Fallback  — the Python running this script
    Result is cached per comfyui_path so switching installations gives the
    correct version without repeated subprocess calls.
    """
    cache_key = comfyui_path or "__none__"
    if cache_key in _py_cache:
        return _py_cache[cache_key]

    # 1. Live API
    if url:
        v = _fetch_comfyui_python(url)
        if v:
            _py_cache[cache_key] = v
            return v

    # 2. On-disk: venv / embedded / conda — delegate to the single source of
    #    truth in pip_checks so conda environments outside the install dir are
    #    also covered (previously fell straight to sys.version for conda users).
    if comfyui_path:
        try:
            from .pip_checks import detect_python_for_root
            _py_path, _py_label = detect_python_for_root(Path(comfyui_path))
            # Only use the result if it's not the system-Python fallback — that
            # fallback is identical to step 3 below and costs an extra subprocess.
            if not _py_label.startswith("system Python"):
                v = _py_ver_exe(str(_py_path))
                if v:
                    _py_cache[cache_key] = v
                    return v
        except Exception:
            pass

    # 3. System fallback
    fallback = sys.version.split()[0]
    _py_cache[cache_key] = fallback
    return fallback

console = Console(highlight=False)


# ── Dynamic URL detection ──────────────────────────────────────────────────────

def _port_from_scripts(comfyui_path: str) -> int | None:
    """
    Return the port for *comfyui_path*, trying in order:

    1. ``--port <n>`` literal in any *.bat / *.cmd / *.sh / *.ps1 in the
       install directory.
    2. Batch/shell variable: ``SET PORT=<n>`` (or any name ending in PORT)
       that is later referenced as ``--port %VAR%`` / ``--port $VAR``.
    3. ComfyUI's own default: ``comfy/cli_args.py`` argparse ``default=``
       for the ``--port`` argument — so if ComfyUI ever changes its built-in
       default this picks it up automatically.

    Rule: scripts and cli_args.py must live inside the install directory.
    No parent-directory guessing, no cross-install contamination.
    """
    import re
    target = Path(comfyui_path).resolve()

    port_literal_re = re.compile(r"--port[=\s]+(\d{2,5})", re.IGNORECASE)
    # Variable reference after --port: %VARNAME% (batch) or $VARNAME / ${VARNAME} (sh)
    port_var_re     = re.compile(r"--port[=\s]+(?:%([^%\s]+)%|\$\{?(\w+)\}?)", re.IGNORECASE)
    # SET/export assignments whose value looks like a port number
    var_assign_re   = re.compile(
        r"(?:set\s+(\w+)\s*=\s*(\d{2,5})|export\s+(\w+)=(\d{2,5}))",
        re.IGNORECASE,
    )

    for pattern in ("*.bat", "*.cmd", "*.sh", "*.ps1"):
        for script in target.glob(pattern):
            try:
                text = script.read_text(encoding="utf-8", errors="ignore")

                # 1. Literal port number
                m = port_literal_re.search(text)
                if m:
                    port = int(m.group(1))
                    if 1 <= port <= 65535:
                        return port

                # 2. Variable reference — resolve from SET/export in same file
                m_var = port_var_re.search(text)
                if m_var:
                    var_name = m_var.group(1) or m_var.group(2) or ""
                    for ma in var_assign_re.finditer(text):
                        name  = ma.group(1) or ma.group(3) or ""
                        value = ma.group(2) or ma.group(4) or ""
                        if name.lower() == var_name.lower() and value:
                            try:
                                port = int(value)
                                if 1 <= port <= 65535:
                                    return port
                            except ValueError:
                                pass
            except Exception:
                pass

    # 3. Fall back to ComfyUI's own argparse default for --port.
    #    This covers installs whose launch scripts omit --port entirely and
    #    rely on ComfyUI's built-in default (typically 8188).
    cli_args_py = target / "comfy" / "cli_args.py"
    if cli_args_py.exists():
        try:
            text = cli_args_py.read_text(encoding="utf-8", errors="ignore")
            m = re.search(
                r'"--port"[^)]*default\s*=\s*(\d{2,5})',
                text,
                re.DOTALL | re.IGNORECASE,
            )
            if m:
                port = int(m.group(1))
                if 1 <= port <= 65535:
                    return port
        except Exception:
            pass

    return None


def _detect_url(comfyui_path: str = "", hint_url: str = "") -> str | None:
    """
    Find the URL for comfyui_path using three strategies, in order:

    A. Live process match (psutil) — only if ComfyUI is running right now:
         1. Python exe is inside the install's venv.
         2. Process cwd equals the install directory.
         3. A cmdline arg resolves to <install>/main.py.
       Port is read from '--port' in cmdline first, then listening connections.
       Returns a verified (HTTP 200) URL.

    B. Static script scan — reads '--port' from bat/sh files in the directory.
       Returns the configured URL regardless of whether ComfyUI is running.

    Returns None only if neither strategy yields anything.
    Never probes random ports — no risk of stealing another install's port.
    """
    import urllib.request

    if not comfyui_path:
        return None

    host = "127.0.0.1"
    if hint_url:
        try:
            host = hint_url.split("://")[1].split(":")[0]
        except Exception:
            pass

    def _check(url: str) -> bool:
        """
        Verify the URL is actually a ComfyUI instance — not just any HTTP 200.
        Checks response structure for ComfyUI-specific fields so another web
        server at the same port can never cause a false match.
        """
        try:
            import json as _json
            with urllib.request.urlopen(f"{url}/system_stats", timeout=1.0) as r:
                if r.status != 200:
                    return False
                data = _json.loads(r.read(8192))
                # ComfyUI's /system_stats always has at least one of these
                return bool(
                    "devices" in data
                    or "python_version" in data
                    or (isinstance(data.get("system"), dict))
                )
        except Exception:
            return False

    # ── A. Live process match ────────────────────────────────────────────────
    try:
        import psutil
        target = Path(comfyui_path).resolve()
        main_py = target / "main.py"

        def _proc_belongs(info: dict) -> bool:
            cmdline = info.get("cmdline") or []
            cwd     = info.get("cwd") or ""

            # 1. Exe is inside this install's venv
            exe = info.get("exe") or ""
            if exe:
                try:
                    if target in Path(exe).resolve().parents:
                        return True
                except Exception:
                    pass

            # 2. CWD is the install directory AND main.py is in the cmdline
            #    (bare cwd match alone would catch pip/pytest/etc. running
            #    from that folder — requiring main.py makes it ComfyUI-specific)
            if cwd:
                try:
                    if Path(cwd).resolve() == target:
                        if any(arg.strip().lower() in ("main.py", "main")
                               for arg in cmdline):
                            return True
                except Exception:
                    pass

            # 3. A cmdline arg resolves to <install>/main.py
            for arg in cmdline:
                try:
                    p = Path(arg)
                    if not p.is_absolute() and cwd:
                        p = Path(cwd) / p
                    if p.resolve() == main_py:
                        return True
                except Exception:
                    pass

            return False

        for proc in psutil.process_iter(["pid", "name", "cwd", "cmdline", "exe"]):
            try:
                if "python" not in (proc.info.get("name") or "").lower():
                    continue
                if not _proc_belongs(proc.info):
                    continue
                # --port in cmdline is fastest
                cmdline = proc.info.get("cmdline") or []
                for i, arg in enumerate(cmdline):
                    if arg == "--port" and i + 1 < len(cmdline):
                        try:
                            port = int(cmdline[i + 1])
                            url = f"http://{host}:{port}"
                            if _check(url):
                                return url
                        except (ValueError, IndexError):
                            pass
                # Fallback: listening connections
                for conn in proc.net_connections(kind="inet"):
                    if conn.status == "LISTEN":
                        url = f"http://{host}:{conn.laddr.port}"
                        if _check(url):
                            return url
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
    except ImportError:
        pass

    # ── B. Static script scan (not running) ─────────────────────────────────
    port = _port_from_scripts(comfyui_path)
    if port:
        return f"http://{host}:{port}"

    return None


# Per-installation URL memory: comfyui_path → last known URL.
# Survives installation switches so each path remembers its own port.
# Also persisted to url_cache.json in the monitor directory so the picker
# can show port metadata even when ComfyUI is not currently running.
_install_urls: dict[str, str] = {}


def _load_url_cache(mdir: Path) -> None:
    """Merge url_cache.json from mdir into _install_urls (non-destructive)."""
    import json as _json
    cache_file = mdir / "url_cache.json"
    if not cache_file.exists():
        return
    try:
        data = _json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    _install_urls.setdefault(k, v)   # live detection wins
    except Exception:
        pass


def _save_url_cache(mdir: Path) -> None:
    """Persist _install_urls to url_cache.json in mdir (atomic write)."""
    import json as _json
    import os as _os
    target = mdir / "url_cache.json"
    tmp    = target.with_suffix(".tmp")
    try:
        tmp.write_text(
            _json.dumps(_install_urls, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ── Package integrity ──────────────────────────────────────────────────────────

_REQUIRED_MODULES = [
    "__init__.py",
    "cli.py",
    "checks.py",
    "env_checks.py",
    "pip_checks.py",
    "backup.py",
    "settings_editor.py",
    "interactive_menu.py",
]

def _package_intact() -> list[str]:
    """
    Return a list of missing module filenames.  Empty list = all present.
    Checks against the package directory at call time so mid-session deletions
    are caught before an action silently fails.
    """
    pkg_dir = Path(__file__).resolve().parent
    return [f for f in _REQUIRED_MODULES if not (pkg_dir / f).exists()]


# ── Startup integrity check ────────────────────────────────────────────────────

_DEFAULT_TOML = """\
# watchupdog configuration
# All values are optional — defaults are used for anything not listed here.

url = "http://127.0.0.1:8188"
interval = 5
timeout  = 5

[thresholds]
queue_warn        = 10
vram_warn_pct     = 90
vram_critical_pct = 97
ram_warn_pct      = 85
disk_warn_gb      = 20
disk_critical_gb  = 5
disk_warn_pct     = 90
disk_critical_pct = 95
stale_job_minutes = 5

[webhooks]
discord_url = ""
ntfy_url    = ""
on_warn     = false
cooldown    = 300
"""


def _startup_check(mdir: Path) -> list[str]:
    """
    Verify the monitor directory has the expected structure and create anything
    missing.  Returns a list of human-readable strings describing what was
    created, for display as a status message.

    Intentionally non-fatal: every action is wrapped so a failure never
    prevents the menu from launching.
    """
    created: list[str] = []

    # backups/ directory — created on demand by backup.py too, but ensuring it
    # exists here means the menu never hits a missing-dir error mid-session.
    backups_dir = mdir / "backups"
    try:
        if not backups_dir.exists():
            backups_dir.mkdir(parents=True, exist_ok=True)
            created.append("created backups/")
    except Exception:
        pass

    # watchupdog.toml — create with commented defaults so users have a template.
    toml_path = mdir / "watchupdog.toml"
    try:
        if not toml_path.exists():
            toml_path.write_text(_DEFAULT_TOML, encoding="utf-8")
            created.append("created watchupdog.toml with defaults")
    except Exception:
        pass

    # url_cache.json — if corrupt (not valid JSON), remove so it is rebuilt.
    cache_path = mdir / "url_cache.json"
    try:
        if cache_path.exists():
            import json as _json
            _json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        try:
            cache_path.unlink()
            created.append("removed corrupt url_cache.json")
        except Exception:
            pass

    # Package module files — flag any that are missing.
    missing = _package_intact()
    if missing:
        created.append(f"WARNING: missing modules: {', '.join(missing)} — press X to repair")

    return created


def _repair(mdir: Path, console: "Console") -> None:
    """
    Integrity check + repair: reports the state of every required module file,
    then reinstalls the package and recreates missing directories/files.
    """
    import subprocess as _sp

    console.print("\n  [bold]watchupdog integrity check[/bold]\n")

    # ── Module file check ──────────────────────────────────────────────────────
    pkg_dir = Path(__file__).resolve().parent
    missing: list[str] = []
    for fname in _REQUIRED_MODULES:
        if (pkg_dir / fname).exists():
            console.print(f"  [green]✓[/green]  {fname}")
        else:
            console.print(f"  [red]✗[/red]  {fname}  [red]missing[/red]")
            missing.append(fname)

    # ── Directory / config check ───────────────────────────────────────────────
    backups_dir = mdir / "backups"
    toml_path   = mdir / "watchupdog.toml"
    backups_unwritable = False
    if not backups_dir.exists():
        console.print("\n  [red]✗[/red]  backups/  [red]missing[/red]")
    else:
        _probe = backups_dir / ".write_probe"
        try:
            _probe.write_text("ok", encoding="utf-8")
            _probe.unlink(missing_ok=True)
            console.print("\n  [green]✓[/green]  backups/")
        except (PermissionError, OSError):
            backups_unwritable = True
            console.print(
                "\n  [yellow]![/yellow]  backups/  "
                "[yellow]exists but not writable — snapshots will fail[/yellow]"
            )
    toml_corrupt = False
    toml_unknown_threshold_keys: list[str] = []
    if toml_path.exists():
        try:
            _toml_text = toml_path.read_text(encoding="utf-8")
            try:
                import tomllib as _tl
            except ImportError:
                import tomli as _tl  # type: ignore[no-redef]
            _toml_data = _tl.loads(_toml_text)
            console.print("  [green]✓[/green]  watchupdog.toml")
            # Check for unrecognised threshold keys — likely typos
            from watchupdog.config import _THRESHOLD_SPECS as _TS
            _user_thresh = _toml_data.get("thresholds", {})
            if isinstance(_user_thresh, dict):
                toml_unknown_threshold_keys = [k for k in _user_thresh if k not in _TS]
            for _uk in toml_unknown_threshold_keys:
                console.print(
                    f"  [yellow]![/yellow]  [thresholds] unknown key: [bold]{_uk}[/bold]  "
                    f"[yellow]— ignored at runtime (possible typo?)[/yellow]"
                )
        except Exception as _toml_err:
            toml_corrupt = True
            console.print(
                f"  [yellow]![/yellow]  watchupdog.toml  "
                f"[yellow]parse error: {_toml_err}[/yellow]"
            )
    else:
        console.print("  [red]✗[/red]  watchupdog.toml  [red]missing[/red]")

    needs_repair = bool(missing) or not backups_dir.exists() or backups_unwritable or not toml_path.exists() or toml_corrupt
    if not needs_repair:
        if toml_unknown_threshold_keys:
            console.print(
                f"\n  [yellow]No structural issues — but {len(toml_unknown_threshold_keys)} unrecognised "
                f"threshold key(s) listed above will be silently ignored at runtime.[/yellow]"
            )
        else:
            console.print("\n  [green]All checks passed — no repair needed.[/green]")
        return

    console.print(
        f"\n  [yellow]{len(missing)} module(s) missing.[/yellow]"
        if missing else "\n  [yellow]Directories or config need rebuilding.[/yellow]"
    )
    console.print()
    try:
        _repair_yn = input("  Run repair now? (y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        _repair_yn = ""
    if _repair_yn != "y":
        console.print("\n  [dim]Repair cancelled.[/dim]")
        return
    console.print("  [dim]Running repair...[/dim]\n")

    # 1. Reinstall the package (only needed when module files are missing)
    pip_ok: bool | None = None   # None = not attempted (no missing modules)
    if missing:
        pkg_src = mdir / "watchupdog"
        if not pkg_src.is_dir():
            console.print(
                f"  [red]✗[/red]  Source directory missing: {pkg_src}\n"
                "  [dim]pip install -e . cannot recreate the package — the source\n"
                "  files are gone.  Re-clone the repository to restore them:[/dim]\n"
                f"  [dim]  git clone https://github.com/fugnsig/watchupdog \"{mdir}\"[/dim]"
            )
            pip_ok = False
        else:
            console.print("  [cyan]Reinstalling package...[/cyan]")
            try:
                result = _sp.run(
                    [sys.executable, "-m", "pip", "install", "-e", str(mdir), "--quiet"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    console.print("  [green]✓[/green]  pip install -e . succeeded")
                    pip_ok = True
                else:
                    console.print(f"  [red]✗[/red]  pip install returned {result.returncode}")
                    if result.stderr.strip():
                        console.print(f"  [dim]{result.stderr.strip()[:200]}[/dim]")
                    pip_ok = False
            except Exception as e:
                console.print(f"  [red]✗[/red]  pip install failed: {e}")
                pip_ok = False

    # 2. Recreate missing directories (independent of pip)
    if backups_unwritable:
        console.print(
            "  [yellow]![/yellow]  backups/ is not writable — cannot fix automatically.\n"
            f"  [dim]Fix permissions: icacls \"{backups_dir}\" /grant %USERNAME%:F[/dim]"
        )
    elif not backups_dir.exists():
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
            console.print("  [green]✓[/green]  created backups/")
        except Exception as e:
            console.print(f"  [red]✗[/red]  could not create backups/: {e}")

    # 3. watchupdog.toml (independent of pip)
    if not toml_path.exists() or toml_corrupt:
        action = "replaced corrupt" if toml_corrupt else "created"
        try:
            toml_path.write_text(_DEFAULT_TOML, encoding="utf-8")
            console.print(f"  [green]✓[/green]  {action} watchupdog.toml with defaults")
        except Exception as e:
            console.print(f"  [red]✗[/red]  could not write watchupdog.toml: {e}")

    # 4. Clear stale URL cache (independent of pip)
    cache_path = mdir / "url_cache.json"
    if cache_path.exists():
        try:
            cache_path.unlink()
            console.print("  [green]✓[/green]  cleared url_cache.json (will rebuild on next launch)")
        except Exception as e:
            console.print(f"  [yellow]![/yellow]  could not clear url_cache.json: {e}")

    global _restart_pending
    if pip_ok is False:
        console.print(
            "\n  [red]Repair incomplete — pip install failed.[/red]"
            "\n  [dim]Try running the launcher as Administrator, or reinstall manually:[/dim]"
            f"\n  [dim]  {sys.executable} -m pip install -e \"{mdir}\"[/dim]"
        )
    else:
        _restart_pending = True
        console.print("\n  [dim]Repair complete.  Restart the launcher to apply changes.[/dim]")


# ── Installation picker ────────────────────────────────────────────────────────

def _pick_installation(installs: list[Path], backups_dir: "Path | None" = None) -> str:
    """
    Full-screen arrow-key picker shown at startup when multiple ComfyUI
    installations are detected.  Returns the chosen path as a string.
    """
    selected = 0

    # Detect URLs in background threads (psutil + script scan, with HTTP verify).
    # _detect_url can take up to ~1 s per install; running in parallel means the
    # picker appears immediately and the metadata column fills in as results land.
    # _url_done tracks whether detection has finished (url=None = not running /
    # no port found in scripts, vs url=None + done=False = still probing).
    _urls:     list[str | None] = [None] * len(installs)
    _url_done: list[bool]       = [False] * len(installs)

    def _detect_for(idx: int, path: Path) -> None:
        path_str = str(path.resolve())
        url = _detect_url(path_str)
        if url is None:
            # Fall back to session/persisted cache (populated on previous runs)
            url = _install_urls.get(path_str) or _install_urls.get(str(path))
        _urls[idx] = url
        _url_done[idx] = True
        if url:
            _install_urls[path_str] = url   # update session cache
            _refresh_connectivity(url)      # warm the connectivity cache

    for _i, _p in enumerate(installs):
        threading.Thread(target=_detect_for, args=(_i, _p), daemon=True).start()

    subtitle = (
        "Multiple installations found — select one to monitor"
        if len(installs) > 1
        else "Select an installation to monitor"
    )

    # Single persistent key-reader thread for the whole picker.
    # Restarted after each consumed keypress so exactly one thread is live.
    _key_q: _queue.Queue[str] = _queue.Queue()

    def _start_reader() -> None:
        try:
            _key_q.put(_get_key())
        except KeyboardInterrupt:
            _key_q.put("\x03")

    _start_reader_thread = lambda: threading.Thread(  # noqa: E731
        target=_start_reader, daemon=True
    ).start()
    _start_reader_thread()

    def _snap() -> tuple:
        """Cheap snapshot of mutable state used to detect when a redraw is needed."""
        return (
            tuple(_url_done),
            tuple(_urls),
            tuple(_conn_state.get(u) for u in _urls),
        )

    cur_snap = _snap()
    cur_size = shutil.get_terminal_size((80, 24))
    _first_paint = True

    while True:
        w  = shutil.get_terminal_size((80, 24)).columns
        ew = min(w, 96)

        # First paint: wipe whatever was on screen (menu or startup message).
        # Subsequent redraws: home only — picker is fixed-height so no bleed.
        if _first_paint:
            _screen_wipe()
            _first_paint = False
        else:
            _win_cls()

        console.print(Panel(
            Text(subtitle, style="dim", justify="center"),
            title="[bold]watchupdog[/bold]",
            border_style="bright_black",
            width=ew,
            padding=(0, 2),
        ))

        table = Table(box=box.SIMPLE, show_header=False, expand=False, padding=(0, 1))
        table.add_column("idx",  width=4)
        table.add_column("path", min_width=30)
        table.add_column("meta")

        # Detect port conflicts: URLs shared by more than one install, but only
        # among completed detections so in-progress probes don't false-alarm.
        _url_owners: dict[str, list[int]] = {}
        for _j, (_uu, _done) in enumerate(zip(_urls, _url_done)):
            if _uu and _done:
                _url_owners.setdefault(_uu, []).append(_j)
        _conflict_urls: set[str] = {
            _uu for _uu, _idxs in _url_owners.items() if len(_idxs) > 1
        }

        for i, path in enumerate(installs):
            _u   = _urls[i]
            _con = _conn_state.get(_u) if _u else None

            # Port — show from detected URL or session cache even when offline
            try:
                _port = f"[dim]:{_u.split(':')[-1].split('/')[0]}[/dim]" if _u else "[dim]no port[/dim]"
            except Exception:
                _port = ""

            # Online status
            if not _url_done[i]:
                # Still probing
                _status_str = "[dim]·[/dim]"
            elif _u and _u in _conflict_urls:
                # Port shared with another install — connectivity state is
                # unreliable; the port belongs to whichever started first.
                _status_str = "[yellow]! port conflict[/yellow]"
            elif _con is True:
                _status_str = "[green]●  online[/green]"
            elif _con is False:
                _status_str = "[dim]○  offline[/dim]"
            elif _u is None:
                # Detection finished: no running process found and no port in scripts
                _status_str = "[dim]○  offline[/dim]"
            else:
                _status_str = "[dim]·[/dim]"

            # Last snapshot date from backups dir
            _snap_str = ""
            if backups_dir and backups_dir.exists():
                try:
                    _name = Path(path).name
                    _snaps = sorted(backups_dir.glob(f"pip_state_{_name}_*.json"))
                    if _snaps:
                        _ts = _snaps[-1].stem.split("_")[-2]   # YYYYMMDD
                        _snap_str = (
                            f"[dim]  snap {_ts[6:8]}/{_ts[4:6]}/{_ts[:4]}[/dim]"
                        )
                except Exception:
                    pass

            meta = f"{_port}  {_status_str}{_snap_str}"

            if i == selected:
                table.add_row(
                    f"[bold cyan]>{i + 1}[/bold cyan]",
                    f"[bold cyan]{path}[/bold cyan]",
                    meta,
                )
            else:
                table.add_row(
                    f"[dim]{i + 1}[/dim]",
                    f"[dim]{path}[/dim]",
                    meta,
                )

        console.print(table)
        if _conflict_urls:
            _conflict_ports = ", ".join(
                sorted(f":{u.split(':')[-1].split('/')[0]}" for u in _conflict_urls)
            )
            console.print(
                f"  [yellow]Warning:[/yellow] multiple installs share port(s) {_conflict_ports}  "
                f"[dim]— only one can run at a time; health data may be from the wrong install[/dim]"
            )
        console.print(
            "\n  [bold]↑ ↓[/bold] navigate   "
            "[bold]Enter[/bold] select"
        )
        sys.stdout.write("\x1b[J")
        sys.stdout.flush()

        # Poll for keypress; also redraw automatically when background thread
        # data changes (url detection, connectivity probes) or terminal resizes.
        # IMPORTANT: _start_reader_thread() is called only when staying in the
        # loop — never before a return.  Arming a reader and then returning
        # leaves a live msvcrt.getch() thread that races with the main menu's
        # reader, splitting a two-byte arrow sequence (\xe0 / P) across both
        # threads and causing the main menu to see a bare "P" keypress.
        key = ""
        while not key:
            try:
                raw = _key_q.get(timeout=0.05)
                if raw == "\x03":
                    raise KeyboardInterrupt
                key = raw
                # Reader thread has exited after delivering the key.
                # Do NOT re-arm here — arm only after confirming we stay in loop.
            except _queue.Empty:
                new_snap = _snap()
                new_size = shutil.get_terminal_size((80, 24))
                if new_snap != cur_snap or new_size != cur_size:
                    cur_snap = new_snap
                    cur_size = new_size
                    break   # exit inner poll → outer loop redraws

        if not key:
            continue   # data changed — redraw, reader thread still live

        if key == "up":
            selected = (selected - 1) % len(installs)
        elif key == "down":
            selected = (selected + 1) % len(installs)
        elif key == "enter":
            return str(installs[selected])   # ← no new reader armed
        elif key == "esc":
            return str(installs[0])          # ← no new reader armed
        # digit shortcut: pressing "2" jumps straight to install #2
        elif key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(installs):
                return str(installs[idx])    # ← no new reader armed

        # Still in the loop — arm next reader now
        _start_reader_thread()


# ── Menu items ─────────────────────────────────────────────────────────────────
# Each entry is either (shortcut_key, label, description) or a str section name.
_ITEMS: list[tuple[str, str, str] | str] = [
    "Monitor",
    ("1", "Installation assessment", "offline · disk, nodes, models, packages"),
    ("2", "Package check",           "torch / pip / GPU compatibility"),
    ("3", "Live dashboard",          "needs ComfyUI running · refreshes every 5s"),
    ("4", "Fix missing packages",    "auto pip install · review changes first"),
    ("5", "HTML report",             "export shareable report.html"),
    "Backup",
    ("6", "Snapshot pip state",      "snapshot before changes"),
    ("7", "Restore pip state",       "roll back to latest snapshot"),
    ("8", "List backups",            "show available snapshots"),
    ("D", "Diff snapshots",          "compare two most recent"),
    ("W", "Backup workflows",        "zip all workflow JSON files"),
    ("M", "Missing models",          "list models removed since last snapshot"),
    "System",
    ("P", "Change port / URL",       ""),   # description filled dynamically
    ("I", "ComfyUI prerequisites",   "clone + venv; large downloads optional"),
    ("N", "New installation",        "clone + venv setup in a new location"),
    ("S", "Settings",                "thresholds, URL, webhooks"),
    *([("A", "Re-launch as Administrator", "Windows · elevate via UAC")] if sys.platform == "win32" else []),
    ("R", "Re-detect ComfyUI",       "rescan installations"),
    ("X", "watchupdog integrity",       "verify core files · reinstall if broken"),
    ("Q", "Quit",                    ""),
]

# shortcut → index for O(1) lookup (section headers skipped)
_KEY_MAP: dict[str, int] = {item[0]: i for i, item in enumerate(_ITEMS) if isinstance(item, tuple)}


# ── Display ────────────────────────────────────────────────────────────────────

def _display_menu(
    comfyui_path: str,
    url: str,
    selected: int,
    status: str = "",
) -> None:
    w   = shutil.get_terminal_size((80, 24)).columns
    ew  = min(w, 96)
    ttl = "watchupdog"

    py_line = _detect_python_info(comfyui_path, url)
    _refresh_connectivity(url)   # trigger background probe; never blocks

    _win_cls()

    active = comfyui_path if comfyui_path else "not detected"

    # Online/offline dot — uses last known state from background probe thread.
    _online = _conn_state.get(url)           # None = not yet probed
    if _online is True:
        _dot = "[green]●[/green]"
    elif _online is False:
        _dot = "[dim]○[/dim]"
    else:
        _dot = "[dim]·[/dim]"               # still probing

    info = Table(box=None, show_header=False, padding=(0, 1), expand=False)
    info.add_column("key",   style="dim",   no_wrap=True)
    info.add_column("value", style="white", no_wrap=True)
    info.add_row("Selected", f"[cyan]{active}[/cyan]" if comfyui_path else "[dim]not detected[/dim]")
    py_color = "green" if (py_line and py_line != "…") else "dim"
    info.add_row(
        "URL",
        f"{_dot} [cyan]{url}[/cyan]  [dim]·  Python[/dim]  [{py_color}]{py_line}[/{py_color}]",
    )

    # ── UAC / privilege warnings ───────────────────────────────────────────────
    _need_admin = False
    if not _priv_limits.get("net_connections", True):
        info.add_row(
            "[yellow]⚠ Privilege[/yellow]",
            "[yellow]Port scanning restricted[/yellow]  "
            "[dim]psutil.net_connections() denied — "
            "ComfyUI auto-detection uses process-cwd fallback.[/dim]",
        )
        _need_admin = True
    if not _priv_limits.get("backup_writable", True):
        info.add_row(
            "[yellow]⚠ Privilege[/yellow]",
            "[yellow]Backup directory not writable[/yellow]  "
            "[dim]pip-state snapshots will fail.[/dim]",
        )
        _need_admin = True
    if _need_admin and sys.platform == "win32" and not _priv_limits.get("is_admin", True):
        info.add_row(
            "[dim]  Tip[/dim]",
            "[dim]Type [bold]A[/bold] to re-launch as Administrator[/dim]",
        )

    console.print(Panel(
        info,
        title=f"[bold]{ttl}[/bold]",
        subtitle="[dim]© Brodie Zotti[/dim]",
        border_style="bright_black",
        width=ew,
        padding=(0, 2),
    ))

    table = Table(box=box.SIMPLE, show_header=False, expand=False, padding=(0, 1))
    table.add_column("key",   width=4)
    table.add_column("label", min_width=28)
    table.add_column("desc")

    for i, item in enumerate(_ITEMS):
        if isinstance(item, str):
            # Section header: "── Name ──────────────────────"
            fill = "─" * max(0, 30 - len(item))
            table.add_row("", f"[dim]── {item} {fill}[/dim]", "")
            continue
        key, label, desc = item
        desc_text = f"current: {url}" if key == "P" else desc
        if i == selected:
            table.add_row(
                f"[bold cyan]>{key}[/bold cyan]",
                f"[bold cyan]{label}[/bold cyan]",
                f"[cyan]{desc_text}[/cyan]",
            )
        else:
            table.add_row(f"[dim]{key}[/dim]", f"[dim]{label}[/dim]", f"[dim]{desc_text}[/dim]")

    console.print(table)
    console.print(
        "  [bold]↑ ↓[/bold] navigate   "
        "[bold]Enter[/bold] select   "
        "or press a shortcut key   "
        "[dim]· section names are labels, not shortcuts[/dim]"
    )
    if _restart_pending:
        console.print(
            "  [bold yellow]⚠ restart pending[/bold yellow]"
            "  [dim]repair was applied — close and re-run the launcher to take effect[/dim]"
        )
    if status:
        console.print(f"  [yellow]{status}[/yellow]")
    # Erase everything below the current cursor position so no stale content
    # from a previous (taller) render bleeds through.
    sys.stdout.write("\x1b[J")
    sys.stdout.flush()



# ── No-install gate ────────────────────────────────────────────────────────────

def _no_install_screen(mdir: Path) -> "str | None":
    """
    Shown when no ComfyUI installation is found before the main menu.
    Returns the confirmed comfyui_path, or None if the user quits.
    Renders in the existing altscreen buffer.
    """
    from rich.panel import Panel

    _hint = ""

    while True:
        _win_cls()
        console.print(Panel(
            "\n  [bold]No ComfyUI installations detected[/bold]\n",
            title="[bold bright_cyan]watchupdog[/bold bright_cyan]",
            border_style="dim",
            padding=(0, 2),
        ))
        console.print()
        if _hint:
            console.print(f"  [yellow]{_hint}[/yellow]")
        else:
            console.print("  [dim]ComfyUI doesn't appear to be installed yet.[/dim]")
        console.print()

        _rows = [
            ("I", "Install ComfyUI",      "clone + venv setup"),
            ("M", "Enter path manually",  "point to an existing install"),
            ("R", "Rescan",               "try detection again"),
            ("Q", "Quit",                 ""),
        ]
        for _k, _lbl, _desc in _rows:
            _d = f"  [dim]{_desc}[/dim]" if _desc else ""
            console.print(f"  [bold cyan]{_k}[/bold cyan]  [white]{_lbl:<24}[/white]{_d}")
        console.print()

        _win_show_cursor()
        try:
            _raw = _get_key()
        except KeyboardInterrupt:
            _win_hide_cursor()
            return None
        _win_hide_cursor()
        _k2 = _raw.upper()

        if _k2 == "I":
            _exit_altscreen()
            _screen_wipe()
            _win_show_cursor()
            _script = mdir / "install_comfyui.py"
            if not _script.exists():
                _script = Path(__file__).resolve().parent.parent / "install_comfyui.py"
            if _script.exists():
                try:
                    subprocess.run([sys.executable, str(_script)])
                except KeyboardInterrupt:
                    pass
            else:
                console.print("\n  [yellow]install_comfyui.py not found.[/yellow]")
                _pause()
            _win_hide_cursor()
            _enter_altscreen()
            _win_cls()
            # Re-scan for the newly created installation
            _post_install_found: "list" = []
            try:
                from watchupdog.env_checks import find_all_comfyui_installs
                _post_install_found = find_all_comfyui_installs()
            except Exception:
                pass

            if len(_post_install_found) == 1:
                return str(_post_install_found[0])
            elif len(_post_install_found) > 1:
                return _pick_installation(_post_install_found)

            # Installation not detected — offer retry or manual path
            console.print(
                "\n  [yellow]ComfyUI was not detected after the installer finished.[/yellow]"
                "\n  This can happen if the installer exited early or cloned to a"
                "\n  custom location."
            )
            console.print()
            console.print("  [bold cyan]R[/bold cyan]  Try the installer again")
            console.print("  [bold cyan]M[/bold cyan]  Enter the path manually")
            console.print("  [bold cyan]Q[/bold cyan]  Quit")
            console.print()
            _win_show_cursor()
            try:
                _after = _get_key().upper()
            except KeyboardInterrupt:
                _after = "Q"
            _win_hide_cursor()
            if _after == "Q":
                return None
            elif _after == "M":
                _k2 = "M"   # fall through to M handler on next loop
                _hint = ""
                continue
            # else R or anything else → loop back to installer

        elif _k2 == "M":
            _win_cls()
            console.print("\n  [cyan]Enter the full path to your ComfyUI folder.[/cyan]")
            console.print("  [dim]Example: C:\\ai\\ComfyUI   or   /home/user/ComfyUI[/dim]\n")
            _win_show_cursor()
            _result = _read_line("  Path (Esc to cancel): ")
            _win_hide_cursor()
            if _result is _ESC or not isinstance(_result, str) or not _result.strip():
                _hint = ""
                continue
            _p = Path(_result.strip())
            if not _p.exists():
                _hint = f"Path not found: {_result.strip()}"
                continue
            # Require either the definitive ComfyUI source file, or the comfy/
            # package directory paired with an entry point.  A bare main.py or
            # requirements.txt alone matches too many non-ComfyUI projects.
            _has_mgmt  = (_p / "comfy" / "model_management.py").is_file()
            _has_comfy = (_p / "comfy").is_dir()
            _has_entry = (_p / "main.py").is_file() or (_p / "server.py").is_file()
            if not (_has_mgmt or (_has_comfy and _has_entry)):
                _missing: list[str] = []
                if not _has_comfy:
                    _missing.append("comfy/")
                if not _has_entry:
                    _missing.append("main.py or server.py")
                _hint = (
                    f"{_p.name!r} doesn't look like a ComfyUI folder "
                    f"(missing: {', '.join(_missing) if _missing else 'comfy/model_management.py'})"
                )
                continue
            return str(_p)

        elif _k2 == "R":
            _win_cls()
            console.print("\n  [dim]Scanning for ComfyUI installations...[/dim]")
            try:
                from watchupdog.env_checks import find_all_comfyui_installs
                _found = find_all_comfyui_installs()
                if len(_found) == 1:
                    return str(_found[0])
                elif len(_found) > 1:
                    return _pick_installation(_found, backups_dir=mdir / "backups")
                else:
                    _hint = "Nothing found — try I to install or M to enter the path manually"
            except Exception:
                _hint = "Scan failed — try M to enter the path manually"

        elif _k2 in ("Q", "\x03"):
            return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clr() -> None:
    _win_cls()   # cursor hidden inside _win_cls


def _pause() -> None:
    """Wait for any key before returning to the menu.

    Uses _wait_key_or_resize with a no-op redraw so that terminal resize events
    are consumed without blanking the screen (the menu re-renders on return).
    """
    _win_show_cursor()
    console.print("\n  Press [bold cyan]any key[/bold cyan] to return to the menu.")
    try:
        _wait_key_or_resize(lambda: None)
    except KeyboardInterrupt:
        pass
    _win_hide_cursor()


def _wait_key_or_resize(
    redraw_fn: Callable[[], None],
    changed_fn: "Callable[[], bool] | None" = None,
) -> str:
    """
    Block until a keypress arrives, repainting on terminal resize or data change.

    Runs _get_key() in a daemon thread and polls every 150 ms.
    redraw_fn() is called on terminal resize.
    If changed_fn is provided and returns True, redraw_fn() is also called so
    that background state (e.g. connectivity probe result) is reflected without
    requiring a keypress.
    Returns the same key strings as _get_key(); raises KeyboardInterrupt for Ctrl-C.
    """
    q: _queue.Queue[str] = _queue.Queue()

    def _reader() -> None:
        try:
            q.put(_get_key())
        except KeyboardInterrupt:
            q.put("\x03")

    threading.Thread(target=_reader, daemon=True).start()

    cur_size = shutil.get_terminal_size()
    while True:
        try:
            key = q.get(timeout=0.15)
        except _queue.Empty:
            new_size = shutil.get_terminal_size()
            if new_size != cur_size:
                cur_size = new_size
                redraw_fn()
            elif changed_fn is not None and changed_fn():
                redraw_fn()
            continue
        if key == "\x03":
            raise KeyboardInterrupt
        return key


def _loading_transition(label: str) -> None:
    """Exit altscreen and print a dim status line before starting an action.

    Matches the dark-grey message style already used throughout the CLI
    (e.g. 'Collecting snapshot — this may take a moment...').
    """
    _exit_altscreen()
    _screen_wipe()
    _win_show_cursor()
    console.print(f"[dim]{label}[/dim]")


def _run_sub(cmd: list[str], label: str = "") -> None:
    """Exit alternate screen, clear the normal screen, run a subprocess, then re-enter.

    Exiting the alt screen before launching prevents watch mode / settings editor
    (which manage their own terminal state) from tearing down the parent's alt
    screen buffer when they call \\x1b[?1049l on exit.

    Wiping the normal screen before the subprocess starts ensures Rich Live
    (used by watch mode) has a clean viewport to anchor its cursor tracking
    from row 1 — without this, any previous output left on the normal screen
    causes Live to lose its position on the first refresh and render a second
    full frame below the first instead of overwriting it.
    """
    if label:
        _loading_transition(label)
    else:
        _exit_altscreen()
        _screen_wipe()
        _win_show_cursor()
    subprocess.run(cmd)
    _win_hide_cursor()
    _enter_altscreen()


def _run_wait(cmd: list[str], label: str = "") -> None:
    """Exit alternate screen, run a subcommand with full scrollback, then wait for a key.

    Wipes the normal screen before launching the subprocess so output always
    starts at the top of the terminal rather than below old scroll history.
    """
    if label:
        _loading_transition(label)
    else:
        _exit_altscreen()
        _screen_wipe()
        _win_show_cursor()
    subprocess.run(cmd)
    _pause()
    _enter_altscreen()


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_menu(
    comfyui_path: str = "",
    url: str = "",
    monitor_dir: str = "",
    launcher: str = "",
) -> None:
    global _launcher_path
    if launcher:
        _launcher_path = launcher

    selected = 0
    status   = ""

    # Clear the normal screen first so any bat/launcher text (e.g. pip output
    # from setup steps) doesn't linger in the normal buffer and reappear when
    # options exit the altscreen.  Then enter the altscreen for the TUI.
    _screen_wipe()
    _enter_altscreen()
    _win_cls()
    console.print("[dim]Starting up...[/dim]")

    # ── Resolve monitor directory early — needed before boot screen ────────────
    env_dir = os.environ.get("MONITOR_DIR", "")
    if monitor_dir:
        mdir = Path(monitor_dir).resolve()
    elif env_dir:
        mdir = Path(env_dir).resolve()
    else:
        # Installed editable: watchupdog/ is inside the monitor dir
        mdir = Path(__file__).resolve().parent.parent

    # ── Startup integrity check ────────────────────────────────────────────────
    _startup_created = _startup_check(mdir)

    # If package files are missing, show the repair screen now — before the
    # installation picker — so the user has a chance to fix things before any
    # module imports are attempted by picker or menu actions.
    if _package_intact():
        _exit_altscreen()
        _screen_wipe()
        _win_show_cursor()
        _repair(mdir, console)
        _pause()
        _enter_altscreen()
        _win_cls()

    # Load persisted URL cache before boot/picker so offline installs show port.
    _load_url_cache(mdir)

    # ── Installation detection ─────────────────────────────────────────────────
    if not comfyui_path:
        comfyui_path = os.environ.get("COMFYUI_PATH", "")

    installs: list = []
    try:
        from watchupdog.env_checks import find_all_comfyui_installs
        installs = find_all_comfyui_installs()
        if len(installs) == 1 and not comfyui_path:
            comfyui_path = str(installs[0])
    except Exception:
        pass

    # ── No-install gate ────────────────────────────────────────────────────────
    # If nothing was found and no path was passed in, show the dedicated screen
    # before the main menu appears. The main menu only opens once a path is set.
    if not comfyui_path and not installs:
        _gate_result = _no_install_screen(mdir)
        if _gate_result is None:
            _exit_altscreen()
            return
        comfyui_path = _gate_result
        installs = [Path(comfyui_path)]

    # ── Dynamic URL detection ──────────────────────────────────────────────────
    detected = _detect_url(comfyui_path, hint_url=url)
    if detected:
        url = detected
        _install_urls[comfyui_path] = url
        _save_url_cache(mdir)
    elif comfyui_path in _install_urls:
        url = _install_urls[comfyui_path]
    else:
        url = "http://127.0.0.1:8188"

    # Altscreen already active — just clear before rendering the menu.

    # Surface anything the startup check created so the user sees it once.
    if _startup_created:
        status = "startup: " + ", ".join(_startup_created)

    # ── Installation picker (multi-install) ────────────────────────────────────
    if len(installs) > 1:
        comfyui_path = _pick_installation(installs, backups_dir=mdir / "backups")
        # Re-detect URL for the installation the user actually selected —
        # the URL block above ran before the picker so it used the wrong path.
        _picked_url = _detect_url(comfyui_path, hint_url=_install_urls.get(comfyui_path, ""))
        if _picked_url:
            url = _picked_url
            _install_urls[comfyui_path] = url
            _save_url_cache(mdir)
        elif comfyui_path in _install_urls:
            url = _install_urls[comfyui_path]
        else:
            url = "http://127.0.0.1:8188"

    def _base() -> list[str]:
        cmd = [sys.executable, "-m", "watchupdog.cli"]
        if comfyui_path:
            cmd += ["--comfyui-path", comfyui_path]
        return cmd

    def _reload_url() -> str:
        """Re-read URL from watchupdog.toml after settings may have changed."""
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore
            cfg_path = mdir / "watchupdog.toml"
            if cfg_path.exists():
                with open(cfg_path, "rb") as f:
                    cfg = tomllib.load(f)
                return cfg.get("url", url)
        except Exception:
            pass
        return url

    def _sync_url_to_toml(current_url: str) -> None:
        """Patch the url field in watchupdog.toml so the settings editor
        always opens with the URL that is actually in use right now."""
        import re as _re
        cfg_path = mdir / "watchupdog.toml"
        try:
            if cfg_path.exists():
                text = cfg_path.read_text(encoding="utf-8")
                if f'url = "{current_url}"' in text:
                    return  # already correct
                new_text = _re.sub(r'^url\s*=.*$', f'url = "{current_url}"',
                                   text, flags=_re.MULTILINE)
                if new_text == text:
                    # url line not found — prepend it
                    new_text = f'url = "{current_url}"\n' + text
                cfg_path.write_text(new_text, encoding="utf-8")
            else:
                cfg_path.write_text(f'url = "{current_url}"\n', encoding="utf-8")
        except Exception:
            pass

    # ── Probe privilege limits once at startup ──────────────────────────────
    # Runs after URL detection so the backup-dir fallback in probe_privilege_limits
    # is already exercised before we try to show warnings.
    global _priv_limits
    try:
        from watchupdog.env_checks import probe_privilege_limits
        _priv_limits = probe_privilege_limits()
    except Exception:
        pass  # keep defaults (all True → no warnings shown)

    while True:
        _display_menu(comfyui_path, url, selected, status)
        status = ""

        # Snapshot connectivity state so we can detect when the background
        # probe finishes and trigger an immediate redraw of the dot.
        _last_conn = [_conn_state.get(url)]

        def _conn_changed() -> bool:
            new = _conn_state.get(url)
            if new != _last_conn[0]:
                _last_conn[0] = new
                return True
            return False

        try:
            key = _wait_key_or_resize(
                lambda: _display_menu(comfyui_path, url, selected, ""),
                changed_fn=_conn_changed,
            )
        except KeyboardInterrupt:
            break

        # ── Navigation ────────────────────────────────────────────────────────
        if key == "up":
            nxt = (selected - 1) % len(_ITEMS)
            while not isinstance(_ITEMS[nxt], tuple):
                nxt = (nxt - 1) % len(_ITEMS)
            selected = nxt
            continue
        if key == "down":
            nxt = (selected + 1) % len(_ITEMS)
            while not isinstance(_ITEMS[nxt], tuple):
                nxt = (nxt + 1) % len(_ITEMS)
            selected = nxt
            continue
        if key == "esc":
            break

        # ── Shortcut key — jump and execute immediately ───────────────────────
        ku = key.upper()
        if key != "enter" and ku in _KEY_MAP:
            selected = _KEY_MAP[ku]
            # fall through to execute

        elif key == "enter":
            pass   # execute current selection

        else:
            continue   # unknown key — ignore

        # ── Execute selected item ─────────────────────────────────────────────
        item_key = _ITEMS[selected][0]

        if item_key == "Q":
            break

        # Guard: check package files are still present before running anything.
        # Skip for X (repair) and Q (quit) which must always be reachable.
        if item_key not in ("Q", "X"):
            _missing = _package_intact()
            if _missing:
                status = (
                    f"[red]Package files missing: {', '.join(_missing)} — "
                    f"press X to repair[/red]"
                )
                continue


        if item_key == "1":
            _run_wait(_base() + ["--env-check"])

        elif item_key == "2":
            _run_wait(_base() + ["--pip-check"])

        elif item_key == "3":
            _run_sub(_base() + ["--watch", "--url", url])

        elif item_key == "4":
            _exit_altscreen()
            _screen_wipe()
            _win_show_cursor()
            console.print("[bold yellow]Fix missing packages[/bold yellow]")
            console.print()
            console.print("  Runs [bold]pip install[/bold] to resolve compatibility issues")
            console.print("  found by the package check. A pip-state snapshot is taken")
            console.print("  automatically before any changes are made.")
            console.print()
            console.print("  [dim]Run option 2 (Package check) first to review what will change.[/dim]")
            console.print()
            try:
                _yn = input("  Proceed with auto-fix? (y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                _yn = ""
            if _yn == "y":
                with console.status(
                    "[dim]Auto-backing up pip state before fix...[/dim]",
                    spinner="dots",
                ):
                    subprocess.run(_base() + ["--backup"], capture_output=True)
                subprocess.run(_base() + ["--pip-check", "--fix"])
                _pause()
            else:
                console.print("\n  [dim]Cancelled.[/dim]")
                _pause()
            _win_hide_cursor()
            _enter_altscreen()

        elif item_key == "5":
            from datetime import datetime as _dt
            _ts        = _dt.now().strftime("%Y%m%d_%H%M")
            _build     = Path(comfyui_path).name if comfyui_path else ""
            _slug      = f"{_build}_{_ts}" if _build else _ts
            _html_name = f"report_{_slug}.html"
            _html_path = mdir / _html_name
            _exit_altscreen()
            _win_cls()
            _win_show_cursor()
            _html_rc = subprocess.run(_base() + ["--url", url, "--html", str(_html_path)]).returncode
            if _html_rc == 0 and _html_path.exists():
                console.print(f"\n  [green]Report saved →[/green] {_html_path}")
                try:
                    if sys.platform == "darwin":
                        subprocess.run(["open", str(_html_path)], check=False)
                    elif sys.platform == "win32":
                        os.startfile(str(_html_path))  # type: ignore[attr-defined]
                    else:
                        subprocess.run(["xdg-open", str(_html_path)], check=False)
                except Exception:
                    pass
            else:
                console.print(
                    f"\n  [red]Report was not saved.[/red]"
                    f"\n  [dim]Check that {mdir} is writable, or run as Administrator.[/dim]"
                )
            _pause()
            _enter_altscreen()

        elif item_key == "6":
            _run_wait(_base() + ["--backup"])

        elif item_key == "7":
            _screen_wipe()
            _win_show_cursor()
            console.print("[bold yellow]Restore pip state from latest snapshot[/bold yellow]")
            console.print()
            console.print("  This will run [bold]pip install --force-reinstall[/bold] on every")
            console.print("  package recorded in the latest snapshot for this installation.")
            console.print("  Installed package versions may change.")
            console.print()
            console.print("  [dim]Run option 6 (Backup) first to snapshot the current state.[/dim]")
            console.print()
            try:
                _yn = input("  Proceed with restore? (y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                _yn = ""
            _win_hide_cursor()
            if _yn == "y":
                _run_wait(_base() + ["--restore", "latest"])
            else:
                console.print("\n  [dim]Restore cancelled.[/dim]")
                _pause()
                _enter_altscreen()

        elif item_key == "8":
            _run_wait(_base() + ["--list-backups"])

        elif item_key == "D":
            _run_wait(_base() + ["--diff", "latest"])

        elif item_key == "W":
            _run_wait(_base() + ["--backup-workflows"])

        elif item_key == "M":
            _run_wait(_base() + ["--missing-models"])

        elif item_key == "P":
            _screen_wipe()
            console.print(f"\n  Current URL: [cyan]{url}[/cyan]")
            console.print(
                "  Examples: 8188  8189  7860  or full http://192.168.1.5:8188\n"
            )
            _win_show_cursor()   # typing field — cursor visible while user types
            result = _read_line(
                "  Enter port number or full URL (Enter = keep, Esc = cancel): "
            )
            _win_hide_cursor()   # back to hidden when done typing
            if result is not _ESC and isinstance(result, str) and result:
                url = (
                    f"http://127.0.0.1:{result}" if result.isdigit() else result
                )
                _install_urls[comfyui_path] = url
                _save_url_cache(mdir)
                _py_cache.pop(comfyui_path or "__none__", None)
                status = f"URL updated → {url}"

        elif item_key == "I":
            # Exit alt screen so the installer's interactive prompts and git/pip
            # output are fully readable in normal screen with scrollback.
            _loading_transition("Preparing installer, checking git and venv...")
            # mdir comes from --monitor-dir (bat file location).
            # If the bat was copied elsewhere, fall back to the package source root.
            install_script = mdir / "install_comfyui.py"
            if not install_script.exists():
                install_script = Path(__file__).resolve().parent.parent / "install_comfyui.py"
            if install_script.exists():
                _idir = install_script.parent
                _win_show_cursor()   # installer uses input() — cursor must be visible
                console.print()
                console.print("[bold]ComfyUI Prerequisites Setup[/bold]")
                console.print("[dim]Sets up or completes your current ComfyUI installation.[/dim]")
                console.print("[dim]Phase 1 (clone + venv) is quick. Phase 2 (PyTorch ≈ 2–4 GB) is optional.[/dim]")
                console.print()
                try:
                    subprocess.run([sys.executable, str(install_script)])
                except KeyboardInterrupt:
                    pass   # Ctrl+C exits the installer but keeps the menu alive
                detect = _idir / "find_comfyui.py"
                if detect.exists():
                    p = subprocess.run(
                        [sys.executable, str(detect)],
                        capture_output=True, text=True,
                    )
                    new = p.stdout.strip()
                    if new:
                        comfyui_path = new
                        status = f"ComfyUI path updated → {comfyui_path}"
            else:
                console.print("[yellow]  install_comfyui.py not found.[/yellow]")
            _pause()
            _enter_altscreen()

        elif item_key == "N":
            # ── New installation — clone a fresh ComfyUI somewhere else ──────────
            # Snapshot the known installs BEFORE running the installer so we can
            # identify exactly which path was just created without guessing.
            _pre_install_paths: set[str] = set()
            try:
                from watchupdog.env_checks import find_all_comfyui_installs as _fai
                _pre_install_paths = {str(p) for p in _fai()}
            except Exception:
                pass

            _loading_transition("Preparing installer for a new ComfyUI clone...")
            _install_script_n = mdir / "install_comfyui.py"
            if not _install_script_n.exists():
                _install_script_n = Path(__file__).resolve().parent.parent / "install_comfyui.py"

            if not _install_script_n.exists():
                console.print("[yellow]  install_comfyui.py not found.[/yellow]")
                _pause()
                _enter_altscreen()
            else:
                _win_show_cursor()
                console.print()
                console.print("[bold]New ComfyUI Installation[/bold]")
                console.print("[dim]Clones ComfyUI into a separate folder. Your current installation is not affected.[/dim]")
                console.print("[dim]You will be asked where to place the new clone.[/dim]")
                console.print()
                try:
                    subprocess.run([sys.executable, str(_install_script_n)])
                except KeyboardInterrupt:
                    pass
                _win_hide_cursor()

                # Identify new installation(s) added since we started
                _new_paths: list[str] = []
                try:
                    _post = {str(p) for p in _fai()}
                    _new_paths = [p for p in _post if p not in _pre_install_paths]
                except Exception:
                    pass

                if _new_paths:
                    _new_path = _new_paths[-1]
                    console.print(
                        f"\n  [green]Installation complete at[/green] "
                        f"[cyan]{_new_path}[/cyan]\n"
                    )
                    console.print(
                        f"  [bold cyan]Y[/bold cyan]  Switch to [white]{Path(_new_path).name}[/white] now\n"
                        f"  [bold cyan]N[/bold cyan]  Keep current "
                        f"([dim]{Path(comfyui_path).name if comfyui_path else 'none'}[/dim])"
                        " — new install appears in picker next launch"
                    )
                    console.print()
                    _win_show_cursor()
                    try:
                        _switch_choice = _get_key().upper()
                    except KeyboardInterrupt:
                        _switch_choice = "N"
                    _win_hide_cursor()

                    if _switch_choice == "Y":
                        comfyui_path = _new_path
                        _py_cache.pop(comfyui_path, None)
                        _switched_url = _detect_url(
                            comfyui_path,
                            hint_url=_install_urls.get(comfyui_path, ""),
                        )
                        url = _switched_url or "http://127.0.0.1:8188"
                        if _switched_url:
                            _install_urls[comfyui_path] = url
                            _save_url_cache(mdir)
                        status = f"Switched to {Path(comfyui_path).name}"
                    else:
                        status = (
                            f"New install at {Path(_new_path).name} — "
                            "will appear in the picker on next launch"
                        )
                else:
                    console.print(
                        "\n  [yellow]New installation not detected after setup.[/yellow]\n"
                        "  Use [bold cyan]R[/bold cyan] to rescan or enter the path via [bold cyan]R[/bold cyan] → manual."
                    )

                _pause()
                _enter_altscreen()

        elif item_key == "A":
            _screen_wipe()
            if sys.platform != "win32":
                console.print("\n  [yellow]Re-launch as Administrator is only available on Windows.[/yellow]")
                _pause()
            elif _priv_limits.get("is_admin", False):
                console.print("\n  [green]Already running as Administrator — no re-launch needed.[/green]")
                _pause()
            else:
                console.print("\n  [cyan]Requesting elevated privileges...[/cyan]")
                if _relaunch_as_admin():
                    # Elevated process is starting — exit this unelevated instance.
                    # Exit the altscreen first, then wipe the normal screen so the
                    # bat's "watchupdog closed." text appears on a clean slate.
                    _exit_altscreen()
                    _screen_wipe()
                    sys.exit(0)
                else:
                    console.print(
                        "  [yellow]Elevation cancelled or failed.[/yellow]  "
                        "Continuing without admin privileges.\n"
                    )
                    _pause()

        elif item_key == "S":
            _sync_url_to_toml(url)   # ensure editor opens with the live URL
            _run_sub(_base() + ["--settings"])
            url = _reload_url()   # pick up any URL change from settings

        elif item_key == "R":
            _screen_wipe()
            console.print("\n  [cyan]Rescanning for ComfyUI installations...[/cyan]\n")
            try:
                from watchupdog.env_checks import find_all_comfyui_installs
                installs = find_all_comfyui_installs()
            except Exception:
                installs = []

            if len(installs) > 1:
                comfyui_path = _pick_installation(installs, backups_dir=mdir / "backups")
            elif len(installs) == 1:
                comfyui_path = str(installs[0])
            else:
                console.print("  [yellow]No ComfyUI installation found automatically.[/yellow]")
                result = _read_line("  Enter path manually (or Enter to skip): ")
                if result is not _ESC and isinstance(result, str) and result.strip():
                    _sp = Path(result.strip())
                    _sp_mgmt  = (_sp / "comfy" / "model_management.py").is_file()
                    _sp_comfy = (_sp / "comfy").is_dir()
                    _sp_entry = (_sp / "main.py").is_file() or (_sp / "server.py").is_file()
                    if _sp.exists() and (_sp_mgmt or (_sp_comfy and _sp_entry)):
                        comfyui_path = str(_sp)
                    else:
                        status = (
                            f"Path rejected — {_sp.name!r} doesn't look like a ComfyUI folder"
                            if _sp.exists() else
                            f"Path not found: {result.strip()}"
                        )
                else:
                    status = "ComfyUI path unchanged."

            if comfyui_path and status != "ComfyUI path unchanged.":
                _py_cache.pop(comfyui_path or "__none__", None)
                detected = _detect_url(comfyui_path, hint_url=_install_urls.get(comfyui_path, ""))
                if detected:
                    url = detected
                    _install_urls[comfyui_path] = url
                    _save_url_cache(mdir)
                    status = f"Selected → {comfyui_path}  ({url})"
                elif comfyui_path in _install_urls:
                    url = _install_urls[comfyui_path]
                    status = f"Selected → {comfyui_path}  (not running, last: {url})"
                else:
                    # No known URL for this install — reset to default so we
                    # never carry over a port from the previously selected install
                    url = "http://127.0.0.1:8188"
                    status = f"Selected → {comfyui_path}  (not running)"

        elif item_key == "X":
            # Run entirely inside the altscreen — _repair() uses capture_output
            # for pip so all output goes through console.print(), never touching
            # the normal screen buffer or polluting scrollback.
            _screen_wipe()
            _win_show_cursor()
            console.print("[dim]Verifying package files and directories...[/dim]\n")
            _repair(mdir, console)
            _pause()
            _win_hide_cursor()
            _win_cls()

    _exit_altscreen()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m watchupdog.interactive_menu",
        description="watchupdog — interactive menu",
    )
    parser.add_argument("--comfyui-path", default="", metavar="PATH",
                        help="Path to ComfyUI installation folder")
    parser.add_argument("--url", default="",
                        help="ComfyUI base URL (auto-detected if omitted)")
    parser.add_argument("--monitor-dir", default="", metavar="DIR",
                        help="Directory containing watchupdog.toml and helper scripts")
    parser.add_argument("--launcher", default="", metavar="FILE",
                        help="Full path to the launcher script that started this process "
                             "(used to re-launch elevated on Windows)")
    args = parser.parse_args()
    run_menu(
        comfyui_path=args.comfyui_path,
        url=args.url,
        monitor_dir=args.monitor_dir,
        launcher=args.launcher,
    )


if __name__ == "__main__":
    main()
