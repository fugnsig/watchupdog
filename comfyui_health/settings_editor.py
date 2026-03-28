"""Interactive settings editor — reads/writes comfyui-health.toml."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich import box

console = Console(highlight=False)

# Sentinel returned by _read_line when the user presses Esc
_ESC = object()

# The settings we expose, in display order
_FIELDS: list[dict[str, Any]] = [
    {"key": "url",                  "label": "ComfyUI URL",          "section": None,         "default": "http://127.0.0.1:8188", "type": str},
    # Derived field — reads/writes the port number embedded in 'url'.
    # Not written as its own TOML key; "derived": True skips it in _save_raw.
    {"key": "_port",                "label": "ComfyUI port",         "section": None,         "default": 8188, "type": int,   "derived": True},
    {"key": "interval",             "label": "Refresh interval (s)", "section": None,         "default": 5,    "type": int,   "min": 1,   "max": 3600},
    {"key": "timeout",              "label": "HTTP timeout (s)",     "section": None,         "default": 5,    "type": int,   "min": 1,   "max": 300},
    {"key": "max_backups",          "label": "Max backups kept",     "section": None,         "default": 10,   "type": int,   "min": 1,   "max": 1000},
    {"key": "vram_warn_pct",        "label": "VRAM warn %",          "section": "thresholds", "default": 90,   "type": float, "min": 0.0, "max": 100.0},
    {"key": "vram_critical_pct",    "label": "VRAM critical %",      "section": "thresholds", "default": 97,   "type": float, "min": 0.0, "max": 100.0},
    {"key": "ram_warn_pct",         "label": "RAM warn %",           "section": "thresholds", "default": 85,   "type": float, "min": 0.0, "max": 100.0},
    {"key": "queue_warn",           "label": "Queue depth warn",     "section": "thresholds", "default": 10,   "type": int,   "min": 1,   "max": 10000},
    {"key": "stale_job_minutes",    "label": "Stale job (min)",      "section": "thresholds", "default": 5,    "type": float, "min": 0.1, "max": 10000.0},
    {"key": "history_jobs",         "label": "History jobs shown",   "section": "thresholds", "default": 50,   "type": int,   "min": 1,   "max": 100000},
    {"key": "discord_url",          "label": "Discord webhook URL",  "section": "webhooks",   "default": "",   "type": str},
    {"key": "ntfy_url",             "label": "ntfy URL",             "section": "webhooks",   "default": "",   "type": str},
    {"key": "on_warn",              "label": "Notify on WARN too",   "section": "webhooks",   "default": False, "type": bool},
    {"key": "min_interval_seconds", "label": "Webhook cooldown (s)", "section": "webhooks",   "default": 300,  "type": int,   "min": 0,   "max": 86400},
]


# ── Low-level keyboard helpers ────────────────────────────────────────────────

def _get_key() -> str:
    """
    Read a single keypress. Returns 'up', 'down', 'enter', 'esc', or a char.

    Windows notes
    -------------
    Two key-sequence formats exist depending on the terminal:
      • \\xe0 H / \\xe0 P  — traditional conhost (cmd.exe, no VT mode)
      • \\x1b [ A / B      — VT sequences (Windows Terminal, VT input mode)

    Windows Terminal enables VT input mode by default, routing arrow keys
    through ReadFile/ReadConsole (what msvcrt.getch uses) rather than the
    ReadConsoleInput event queue.  The \\x1b byte arrives first; we sleep
    100 ms to let the rest of the sequence land before draining the buffer.
    100 ms is imperceptible to humans.
    """
    if os.name == "nt":
        import msvcrt
        import time as _time

        ch = msvcrt.getch()

        # Traditional extended-key prefix (cmd.exe / conhost without VT mode)
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            if ch2 == b"H": return "up"
            if ch2 == b"P": return "down"
            return "other"

        # ESC byte: plain Esc OR start of a VT sequence (\x1b[A / \x1b[B)
        if ch == b"\x1b":
            _time.sleep(0.10)           # let the rest of any sequence arrive
            seq = b""
            while msvcrt.kbhit():
                seq += msvcrt.getch()
            if not seq:
                return "esc"
            # CSI sequences: ESC [ <optional-params> <final-letter>
            # Arrow up   = \x1b[A  or  \x1b[1;nA  etc. — final byte is always b"A"
            # Arrow down = \x1b[B  or  \x1b[1;nB  etc. — final byte is always b"B"
            if seq.startswith(b"[") and len(seq) >= 2:
                final = seq[-1:]
                if final == b"A": return "up"
                if final == b"B": return "down"
            return "other"

        if ch in (b"\r", b"\n"): return "enter"
        if ch == b"\x03":        raise KeyboardInterrupt
        try:    return ch.decode("utf-8")
        except: return "other"

    else:
        # Linux / macOS — raw terminal mode
        import tty, termios
        import select as _sel
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ready, _, _ = _sel.select([sys.stdin], [], [], 0.10)
                if ready:
                    ch2 = sys.stdin.read(1)
                    if ch2 == "[":
                        # Drain any remaining CSI parameter bytes, then read final
                        seq = ""
                        while True:
                            r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                            if not r:
                                break
                            c = sys.stdin.read(1)
                            seq += c
                            if c.isalpha():   # final byte of CSI sequence
                                break
                        if seq.endswith("A"): return "up"
                        if seq.endswith("B"): return "down"
                    return "other"
                return "esc"
            if ch in ("\r", "\n"): return "enter"
            if ch == "\x03":       raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_line(prompt: str) -> str | object:
    """
    Show prompt and read a line of text with manual echo.
    Returns the stripped string, or _ESC if the user pressed Esc.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    if os.name == "nt":
        import msvcrt
        import time as _time
        buf: list[str] = []
        while True:
            # getwch() returns a single Unicode str character — the only correct
            # way to handle non-ASCII paths (CJK, accented chars, etc.).
            # getch() reads one byte at a time, silently dropping multi-byte
            # UTF-8 sequences when decode() fails on the first byte.
            ch = msvcrt.getwch()
            if ch == "\x1b":
                _time.sleep(0.05)                       # let VT sequence chars arrive
                while msvcrt.kbhit(): msvcrt.getwch()   # drain them
                sys.stdout.write("\n"); sys.stdout.flush()
                return _ESC
            if ch in ("\r", "\n"):
                sys.stdout.write("\n"); sys.stdout.flush()
                return "".join(buf).strip()
            if ch in ("\b", "\x7f"):
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b"); sys.stdout.flush()
            elif ch in ("\x00", "\xe0"):
                msvcrt.getwch()                         # discard second char of two-part key
            elif ch >= " ":
                buf.append(ch)
                sys.stdout.write(ch); sys.stdout.flush()
    else:
        import tty, termios
        import select as _sel
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        buf: list[str] = []
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    ready, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                    if ready:
                        ch2 = sys.stdin.read(1)
                        if ch2 == "[" and _sel.select([sys.stdin], [], [], 0.05)[0]:
                            sys.stdin.read(1)           # discard direction char
                    else:
                        sys.stdout.write("\r\n"); sys.stdout.flush()
                        return _ESC
                elif ch in ("\r", "\n"):
                    sys.stdout.write("\r\n"); sys.stdout.flush()
                    return "".join(buf).strip()
                elif ch in ("\b", "\x7f"):
                    if buf:
                        buf.pop()
                        sys.stdout.write("\b \b"); sys.stdout.flush()
                elif ch == "\x03":
                    raise KeyboardInterrupt
                elif ch >= " ":
                    buf.append(ch)
                    sys.stdout.write(ch); sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Config I/O ────────────────────────────────────────────────────────────────

def _config_path() -> Path:
    """Return the config path to read from / write to.

    Mirrors config.py's load order exactly so the editor always touches the
    same file that the monitor loaded:
      1. $MONITOR_DIR/comfyui-health.toml
      2. ./comfyui-health.toml
      3. ~/.config/comfyui-health/comfyui-health.toml
    When no file exists yet, falls back to the local path (option 2).
    """
    candidates: list[Path] = []
    env = os.environ.get("MONITOR_DIR")
    if env:
        candidates.append(Path(env) / "comfyui-health.toml")
    candidates.append(Path("comfyui-health.toml"))
    candidates.append(Path.home() / ".config" / "comfyui-health" / "comfyui-health.toml")
    for p in candidates:
        if p.exists():
            return p
    return Path("comfyui-health.toml")


def _load_raw() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _get_value(raw: dict[str, Any], field: dict[str, Any]) -> Any:
    if field.get("derived"):
        # Extract port from the url field
        url = raw.get("url", "http://127.0.0.1:8188")
        m = re.search(r":(\d+)$", url.rstrip("/"))
        return int(m.group(1)) if m else 8188
    section = field["section"]
    key     = field["key"]
    if section:
        return raw.get(section, {}).get(key, field["default"])
    return raw.get(key, field["default"])


def _set_value(raw: dict[str, Any], field: dict[str, Any], value: Any) -> None:
    if field.get("derived"):
        # Rewrite the port in the url field, preserving host and scheme
        url = raw.get("url", "http://127.0.0.1:8188").rstrip("/")
        raw["url"] = re.sub(r":\d+$", f":{value}", url)
        return
    section = field["section"]
    key     = field["key"]
    if section:
        raw.setdefault(section, {})[key] = value
    else:
        raw[key] = value


def _toml_value(v: Any) -> str:
    """Serialise a scalar or list value to an inline TOML string."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(x) for x in v)
        return f"[{items}]"
    return str(v)


def _save_raw(raw: dict[str, Any]) -> Path:
    """Serialise *raw* back to TOML and write to the config path.

    Keys and sections that are not listed in _FIELDS (e.g. ``expected_models``,
    ``comfyui_path``, or any user-added entries) are preserved verbatim at the
    end of the file.  Previously, unknown keys were silently dropped on every
    save — this was a data-loss bug.
    """
    path  = _config_path()
    lines: list[str] = ["# comfyui-health configuration\n"]

    # Track which top-level keys / sections _FIELDS already covers.
    written_keys:     set[str] = set()
    written_sections: set[str] = set()

    # ── Top-level scalar keys from _FIELDS ───────────────────────────────────
    for f in _FIELDS:
        if f.get("derived"):
            continue          # port is stored inside url, not as its own key
        if f["section"] is None:
            written_keys.add(f["key"])
            v = raw.get(f["key"], f["default"])
            lines.append(f'{f["key"]} = {_toml_value(v)}\n')

    # ── Sections from _FIELDS ─────────────────────────────────────────────────
    for f in _FIELDS:
        sec = f["section"]
        if sec and sec not in written_sections:
            written_sections.add(sec)
            lines.append(f"\n[{sec}]\n")
            for sf in _FIELDS:
                if sf["section"] == sec:
                    v = raw.get(sec, {}).get(sf["key"], sf["default"])
                    lines.append(f'{sf["key"]} = {_toml_value(v)}\n')

    # ── Preserve any keys / sections not managed by _FIELDS ──────────────────
    # This ensures user-authored sections like [expected_models] or custom
    # top-level keys are never silently discarded when settings are saved.
    for key, val in raw.items():
        if isinstance(val, dict):
            if key not in written_sections:
                lines.append(f"\n[{key}]\n")
                for sk, sv in val.items():
                    lines.append(f"{sk} = {_toml_value(sv)}\n")
        else:
            if key not in written_keys:
                lines.append(f"{key} = {_toml_value(val)}\n")

    try:
        path.write_text("".join(lines), encoding="utf-8")
    except (PermissionError, OSError) as e:
        raise PermissionError(
            f"Cannot save settings to {path}: {e}.  "
            f"Run as Administrator or check file permissions."
        ) from e
    return path


# ── Display ───────────────────────────────────────────────────────────────────

def _cursor(show: bool) -> None:
    """Show or hide the terminal cursor — platform-agnostic."""
    if os.name == "nt":
        try:
            import ctypes
            class _CI(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.c_ulong), ("bVisible", ctypes.c_bool)]
            h  = ctypes.windll.kernel32.GetStdHandle(-11)
            ci = _CI()
            if ctypes.windll.kernel32.GetConsoleCursorInfo(h, ctypes.byref(ci)):
                ci.bVisible = show
                ctypes.windll.kernel32.SetConsoleCursorInfo(h, ctypes.byref(ci))
        except Exception:
            pass
    else:
        sys.stdout.write("\x1b[?25h" if show else "\x1b[?25l")
        sys.stdout.flush()


def _display(raw: dict[str, Any], selected: int, status: str = "", full_clear: bool = False) -> None:
    _cursor(False)   # keep cursor hidden while redrawing
    if full_clear:
        # First render: erase visible screen + scrollback so no previous output
        # bleeds through above the settings table.
        sys.stdout.write("\x1b[?25l\x1b[2J\x1b[3J\x1b[H")
    else:
        # Navigation redraws: move to top-left and overwrite in place.
        # \x1b[2J on every keypress would push a copy into the scrollback
        # on Windows Terminal, producing the "prints to cmd" scrolling effect.
        sys.stdout.write("\x1b[?25l\x1b[H")
    sys.stdout.flush()

    table = Table(
        title="[bold]ComfyUI Health Monitor — Settings[/bold]",
        box=box.SIMPLE_HEAD,
        expand=True,
        show_lines=False,
    )
    table.add_column("#", width=4)
    table.add_column("Setting", min_width=24)
    table.add_column("Current value")
    table.add_column("Section", style="dim")

    for i, f in enumerate(_FIELDS, 1):
        v = _get_value(raw, f)
        display_val = str(v) if v != "" else "[dim](not set)[/dim]"
        if isinstance(v, bool):
            display_val = "[green]yes[/green]" if v else "[dim]no[/dim]"
        elif f["type"] == str and v:
            display_val = f"[cyan]{v}[/cyan]"

        is_sel = (i - 1) == selected
        num    = f"[bold cyan]>>{i}[/bold cyan]" if is_sel else f"  {i}"
        lbl    = f["label"]
        label_cell = f"[bold cyan]{lbl}[/bold cyan]" if is_sel else lbl
        table.add_row(
            num,
            label_cell,
            display_val,
            f["section"] or "",
        )

    console.print(table)
    # Always print status line to keep total line count constant — required for
    # clean in-place overwrites (blank line on no-status redraws).
    console.print(f"  [yellow]{status}[/yellow]" if status else "")
    console.print("  [bold]↑ ↓[/bold] navigate   [bold]Enter[/bold] edit   [bold]Esc[/bold] save & return to menu")


# ── Main editor loop ──────────────────────────────────────────────────────────

def run_settings_editor() -> None:
    raw        = _load_raw()
    selected   = 0
    status     = ""
    full_clear = True   # first render always does a full clear + scrollback wipe

    while True:
        _display(raw, selected, status, full_clear=full_clear)
        full_clear = False   # subsequent navigation redraws overwrite in place
        status     = ""

        try:
            key = _get_key()
        except KeyboardInterrupt:
            break

        if key == "esc":
            break
        elif key == "up":
            selected = (selected - 1) % len(_FIELDS)
        elif key == "down":
            selected = (selected + 1) % len(_FIELDS)
        elif key == "enter":
            full_clear = True   # edit prompt prints extra lines; need full clear on redraw
            field   = _FIELDS[selected]
            current = _get_value(raw, field)
            console.print(f"\n  [bold]{field['label']}[/bold]  (current: [cyan]{current}[/cyan])")

            if field["type"] == bool:
                console.print("  y / n   (Enter = keep   Esc = cancel):")
                _cursor(True)
                result = _read_line("  > ")
                _cursor(False)
                if result is _ESC:
                    status = "Edit cancelled."
                elif isinstance(result, str):
                    val = result.lower()
                    if val in ("y", "yes", "1", "true"):
                        _set_value(raw, field, True)
                    elif val in ("n", "no", "0", "false"):
                        _set_value(raw, field, False)
            else:
                type_hint = {"int": "integer", "float": "number", "str": "text"}.get(
                    field["type"].__name__, "value"
                )
                console.print(f"  Enter new {type_hint}   (Enter = keep   Esc = cancel):")
                _cursor(True)
                result = _read_line("  > ")
                _cursor(False)
                if result is _ESC:
                    status = "Edit cancelled."
                elif isinstance(result, str) and result:
                    try:
                        typed = field["type"](result)
                        if field.get("derived") and not (1 <= int(typed) <= 65535):
                            status = "Port must be 1–65535 — value not changed."
                        elif "min" in field and typed < field["min"]:
                            status = f"Value must be ≥ {field['min']} — not changed."
                        elif "max" in field and typed > field["max"]:
                            status = f"Value must be ≤ {field['max']} — not changed."
                        else:
                            _set_value(raw, field, typed)
                    except ValueError:
                        status = f"Invalid {type_hint} — value not changed."

        elif key.isdigit() and key != "0":
            idx = int(key) - 1
            if 0 <= idx < len(_FIELDS):
                selected = idx
            else:
                status = f"No setting #{key}."

    _cursor(False)
    try:
        saved = _save_raw(raw)
        console.print(f"\n[green]  Settings saved →[/green] {saved.resolve()}\n")
    except PermissionError as e:
        console.print(f"\n[red]  Cannot save settings:[/red] {e}\n")
