"""Interactive arrow-key driven main menu for ComfyUI Health Monitor."""

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
    params = " ".join(f'"{a}"' for a in ["-m", "comfyui_health.interactive_menu"] + extra)
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
        """Hide cursor and home — content is overwritten in-place, no blank-screen wipe."""
        _cursor_visible(False)
        try:
            sys.stdout.write("\x1b[?25l\x1b[H")
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

    def _enter_altscreen() -> None:   # type: ignore[misc]
        sys.stdout.write("\x1b[?1049h\x1b[?25l"); sys.stdout.flush()

    def _exit_altscreen() -> None:    # type: ignore[misc]
        sys.stdout.write("\x1b[?1049l\x1b[?25h"); sys.stdout.flush()

from rich.console import Console
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from comfyui_health.settings_editor import _get_key, _read_line, _ESC


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

    # 2. On-disk venv / embedded Python inside the ComfyUI folder
    if comfyui_path:
        p = Path(comfyui_path)
        for venv_dir in ("venv", ".venv", "python_embeded", "python"):
            for rel in (
                f"{venv_dir}/Scripts/python.exe",
                f"{venv_dir}/bin/python3",
                f"{venv_dir}/bin/python",
                f"{venv_dir}/python.exe",
            ):
                full = p / rel
                if full.exists():
                    v = _py_ver_exe(str(full))
                    if v:
                        _py_cache[cache_key] = v
                        return v

    # 3. System fallback
    fallback = sys.version.split()[0]
    _py_cache[cache_key] = fallback
    return fallback

console = Console(highlight=False)


# ── Dynamic URL detection ──────────────────────────────────────────────────────

def _port_from_scripts(comfyui_path: str) -> int | None:
    """
    Scan launch scripts INSIDE comfyui_path for '--port <n>'.

    Rule: bat/sh file location == installation location.
    We only read scripts that live directly in the install directory —
    no parent-directory guessing, no cross-install contamination.
    """
    import re
    target = Path(comfyui_path).resolve()
    port_re = re.compile(r"--port[=\s]+(\d{4,5})")
    for pattern in ("*.bat", "*.cmd", "*.sh", "*.ps1"):
        for script in target.glob(pattern):
            try:
                text = script.read_text(encoding="utf-8", errors="ignore")
                m = port_re.search(text)
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


# ── Installation picker ────────────────────────────────────────────────────────

def _pick_installation(installs: list[Path]) -> str:
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

    while True:
        w  = shutil.get_terminal_size((80, 24)).columns
        ew = min(w, 96)

        _win_cls()

        console.print(Panel(
            Text(subtitle, style="dim", justify="center"),
            title="[bold]ComfyUI Health Monitor[/bold]",
            border_style="bright_black",
            width=ew,
            padding=(0, 2),
        ))

        table = Table(box=box.SIMPLE, show_header=False, expand=False, padding=(0, 1))
        table.add_column("idx",  width=4)
        table.add_column("path", min_width=30)
        table.add_column("meta")

        for i, path in enumerate(installs):
            url  = _urls[i]
            if url and _conn_state.get(url) is True:
                try:
                    port_str = f":{url.split(':')[-1].split('/')[0]}"
                except Exception:
                    port_str = ""
                meta = f"[dim]{port_str}  ·  [/dim][green]online[/green]"
            else:
                meta = ""

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
    ("S", "Settings",                "thresholds, URL, webhooks"),
    *([("A", "Re-launch as Administrator", "Windows · elevate via UAC")] if sys.platform == "win32" else []),
    ("R", "Re-detect ComfyUI",       "rescan installations"),
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
    ttl = "ComfyUI Health Monitor"

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
    if status:
        console.print(f"  [yellow]{status}[/yellow]")
    # Erase everything below the current cursor position so no stale content
    # from a previous (taller) render bleeds through.
    sys.stdout.write("\x1b[J")
    sys.stdout.flush()



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


def _wait_key_or_resize(redraw_fn: Callable[[], None]) -> str:
    """
    Block until a keypress arrives, repainting on terminal resize.

    Runs _get_key() in a daemon thread and polls terminal size every 150 ms.
    If the terminal is resized while waiting, redraw_fn() is called immediately
    so the menu adapts to the new dimensions without needing a keypress.
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
            continue
        if key == "\x03":
            raise KeyboardInterrupt
        return key


def _run_sub(cmd: list[str]) -> None:
    """Exit alternate screen, clear the normal screen, run a subprocess, then re-enter.

    Exiting the alt screen before launching prevents watch mode / settings editor
    (which manage their own terminal state) from tearing down the parent's alt
    screen buffer when they call \\x1b[?1049l on exit.

    Clearing the normal screen (\\x1b[2J\\x1b[H) before the subprocess starts
    ensures Rich Live (used by watch mode) has a clean viewport to anchor its
    cursor tracking from row 1 — without this, any previous output left on the
    normal screen causes Live to lose its position on the first refresh and
    render a second full frame below the first instead of overwriting it.
    """
    _exit_altscreen()
    _win_cls()
    _win_show_cursor()
    subprocess.run(cmd)
    _win_hide_cursor()
    _enter_altscreen()


def _run_wait(cmd: list[str]) -> None:
    """Exit alternate screen, run a subcommand with full scrollback, then wait for a key.

    Clears the normal screen and homes the cursor before launching the subprocess
    so output always starts at the top of the terminal rather than below old
    scroll history.  _win_cls() uses the Win32 console API on Windows (which
    guarantees the viewport is blank) and falls back to VT sequences elsewhere.
    """
    _exit_altscreen()
    _win_cls()
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

    _screen_wipe()
    _enter_altscreen()

    # ── Resolve monitor directory early — needed before the picker ─────────────
    env_dir = os.environ.get("MONITOR_DIR", "")
    if monitor_dir:
        mdir = Path(monitor_dir).resolve()
    elif env_dir:
        mdir = Path(env_dir).resolve()
    else:
        # Installed editable: comfyui_health/ is inside the monitor dir
        mdir = Path(__file__).resolve().parent.parent

    # Load persisted URL cache so the picker can show port metadata even when
    # ComfyUI is not currently running (populated on first successful detection).
    _load_url_cache(mdir)

    # ── Installation picker ────────────────────────────────────────────────────
    # If no path was passed as an argument, fall back to the COMFYUI_PATH env
    # var (set by the bat launcher after running find_comfyui.py).
    if not comfyui_path:
        comfyui_path = os.environ.get("COMFYUI_PATH", "")

    # Always run detection so the user can choose when multiple installs exist.
    # Single install with no path already set → auto-assign silently.
    try:
        from comfyui_health.env_checks import find_all_comfyui_installs
        installs = find_all_comfyui_installs()
        if len(installs) > 1:
            comfyui_path = _pick_installation(installs)
        elif len(installs) == 1 and not comfyui_path:
            comfyui_path = str(installs[0])
    except Exception:
        pass   # detection failed — use whatever was resolved above

    # ── Dynamic URL detection ──────────────────────────────────────────────────
    # Priority: live psutil match → per-install memory → config hint → placeholder
    # Always reassign url so switching installs never inherits a previous port.
    detected = _detect_url(comfyui_path, hint_url=url)
    if detected:
        url = detected
        _install_urls[comfyui_path] = url
        _save_url_cache(mdir)
    elif comfyui_path in _install_urls:
        url = _install_urls[comfyui_path]
    else:
        url = "http://127.0.0.1:8188"       # placeholder — set via P when running

    def _base() -> list[str]:
        cmd = [sys.executable, "-m", "comfyui_health.cli"]
        if comfyui_path:
            cmd += ["--comfyui-path", comfyui_path]
        return cmd

    def _reload_url() -> str:
        """Re-read URL from comfyui-health.toml after settings may have changed."""
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore
            cfg_path = mdir / "comfyui-health.toml"
            if cfg_path.exists():
                with open(cfg_path, "rb") as f:
                    cfg = tomllib.load(f)
                return cfg.get("url", url)
        except Exception:
            pass
        return url

    def _sync_url_to_toml(current_url: str) -> None:
        """Patch the url field in comfyui-health.toml so the settings editor
        always opens with the URL that is actually in use right now."""
        import re as _re
        cfg_path = mdir / "comfyui-health.toml"
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
        from comfyui_health.env_checks import probe_privilege_limits
        _priv_limits = probe_privilege_limits()
    except Exception:
        pass  # keep defaults (all True → no warnings shown)

    while True:
        _display_menu(comfyui_path, url, selected, status)
        status = ""

        try:
            key = _wait_key_or_resize(
                lambda: _display_menu(comfyui_path, url, selected, "")
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

        elif item_key == "1":
            # Offline installation assessment — no ComfyUI needed
            _run_wait(_base() + ["--env-check"])

        elif item_key == "2":
            _run_wait(_base() + ["--pip-check"])

        elif item_key == "3":
            # Live dashboard — exits on its own when user presses Esc
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
            _exit_altscreen()
            _win_cls()
            _win_show_cursor()
            # mdir comes from --monitor-dir (bat file location).
            # If the bat was copied elsewhere, fall back to the package source root.
            install_script = mdir / "install_comfyui.py"
            if not install_script.exists():
                install_script = Path(__file__).resolve().parent.parent / "install_comfyui.py"
            if install_script.exists():
                _idir = install_script.parent
                _win_show_cursor()   # installer uses input() — cursor must be visible
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

        elif item_key == "A":
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
            console.print("\n  [cyan]Rescanning for ComfyUI installations...[/cyan]\n")
            try:
                from comfyui_health.env_checks import find_all_comfyui_installs
                installs = find_all_comfyui_installs()
            except Exception:
                installs = []

            if len(installs) > 1:
                comfyui_path = _pick_installation(installs)
            elif len(installs) == 1:
                comfyui_path = str(installs[0])
            else:
                console.print("  [yellow]No ComfyUI installation found automatically.[/yellow]")
                result = _read_line("  Enter path manually (or Enter to skip): ")
                if result is not _ESC and isinstance(result, str) and result:
                    comfyui_path = result
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

    _exit_altscreen()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m comfyui_health.interactive_menu",
        description="ComfyUI Health Monitor — interactive menu",
    )
    parser.add_argument("--comfyui-path", default="", metavar="PATH",
                        help="Path to ComfyUI installation folder")
    parser.add_argument("--url", default="",
                        help="ComfyUI base URL (auto-detected if omitted)")
    parser.add_argument("--monitor-dir", default="", metavar="DIR",
                        help="Directory containing comfyui-health.toml and helper scripts")
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
