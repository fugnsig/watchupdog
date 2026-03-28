"""
ComfyUI installer — preset-driven.

Phase 1  (always runs when internet + git are available):
  • User picks a preset  (minimal / nunchaku / custom .toml / bare)
  • git clone all repos declared in the preset
  • create .venv
  • pip install lightweight base deps only  (no torch)

Phase 2  (optional — user is asked before any large download):
  • pip install -r requirements.txt  (includes PyTorch, ~2-4 GB)
  • user picks GPU backend

Called by interactive_menu.py (options I and N).
The install path is printed to stdout so the caller can update its state.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

try:
    from rich.console import Console
    console = Console(highlight=False)
except ImportError:
    class _FallbackConsole:  # type: ignore[no-redef]
        def print(self, *a, **kw):
            import builtins
            builtins.print(*[str(x) for x in a])
    console = _FallbackConsole()  # type: ignore[assignment]

try:
    import tomllib                      # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib         # type: ignore[no-redef]
    except ImportError:
        tomllib = None                  # type: ignore[assignment]

# Fallback if neither tomllib nor tomli is available: parse just what we need.
def _parse_toml_fallback(text: str) -> dict:
    """Minimal TOML parser: handles string scalars and [[array-of-tables]]."""
    import re
    result: dict = {}
    current_array: list | None = None
    current_table: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            key = line.strip("[]").strip()
            if key not in result or not isinstance(result[key], list):
                result[key] = []
            current_table = {}
            result[key].append(current_table)
            current_array = result[key]
            continue
        if line.startswith("["):
            current_table = None
            current_array = None
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if current_table is not None:
                current_table[k] = v
            else:
                result[k] = v
    return result

_ESC = object()

_TORCH_BACKENDS: list[tuple[str, str, str | None]] = [
    ("1", "CUDA 12.4  (RTX 40-series, RTX 30-series, newer)",
     "https://download.pytorch.org/whl/cu124"),
    ("2", "CUDA 11.8  (older NVIDIA GPUs)",
     "https://download.pytorch.org/whl/cu118"),
    ("3", "ROCm 6.1   (AMD GPUs, Linux only)",
     "https://download.pytorch.org/whl/rocm6.1"),
    ("4", "CPU only   (no GPU acceleration)", ""),
    ("5", "Skip — I will install PyTorch manually", None),
]

# ── Preset helpers ──────────────────────────────────────────────────────────────

def _load_toml(path: Path) -> dict:
    if tomllib is not None:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    return _parse_toml_fallback(path.read_text(encoding="utf-8"))


def _load_preset(path: Path, presets_dir: Path) -> list[dict]:
    """
    Load a preset .toml and resolve its `extends` chain.
    Returns a flat ordered list of repo dicts.
    """
    data  = _load_toml(path)
    repos: list[dict] = []

    parent_name = data.get("extends", "")
    if parent_name:
        parent_path = presets_dir / f"{parent_name}.toml"
        if parent_path.exists():
            repos = _load_preset(parent_path, presets_dir)
        else:
            console.print(f"  [yellow]Warning: preset '{parent_name}' not found — skipping extends.[/yellow]")

    repos.extend(data.get("repos", []))
    return repos


def _pick_preset(presets_dir: Path) -> "list[dict] | None":
    """
    Show available presets and return the resolved repo list.
    Returns None on Esc / cancel, [] for bare (no preset).
    """
    available = sorted(presets_dir.glob("*.toml")) if presets_dir.exists() else []
    # Filter out presets that only extend another (they appear via the parent)
    top_level = []
    for p in available:
        try:
            if not _load_toml(p).get("extends"):
                top_level.append(p)
        except Exception:
            pass

    console.print("\n[bold]Choose a setup preset:[/bold]\n")
    options: list[tuple[str, str]] = []
    for p in top_level:
        try:
            desc = _load_toml(p).get("description", "")
        except Exception:
            desc = ""
        options.append((p.stem, desc))
        idx = len(options)
        console.print(f"  [bold cyan]{idx}[/bold cyan]  {p.stem:<22} [dim]{desc}[/dim]")

    n = len(options)
    console.print(f"  [bold cyan]{n+1}[/bold cyan]  {'Custom':<22} [dim]path to your own .toml file[/dim]")
    console.print(f"  [bold cyan]{n+2}[/bold cyan]  {'Bare install':<22} [dim]ComfyUI only, no extras[/dim]")
    console.print()

    raw = _read_input(f"  Choice (1–{n+2}, default 1, Esc to cancel): ")
    if raw is _ESC:
        return None
    assert isinstance(raw, str)
    choice = (raw.strip() or "1")

    try:
        idx = int(choice) - 1
    except ValueError:
        idx = 0

    if 0 <= idx < n:
        return _load_preset(top_level[idx], presets_dir)
    elif idx == n:
        # Custom path
        path_raw = _read_input("  Path to preset .toml (Esc to cancel): ")
        if path_raw is _ESC or not isinstance(path_raw, str) or not path_raw.strip():
            return None
        custom = Path(path_raw.strip())
        if not custom.exists():
            console.print(f"  [yellow]File not found: {custom}[/yellow]")
            return None
        return _load_preset(custom, custom.parent)
    else:
        # Bare install — just ComfyUI core
        return [{"name": "ComfyUI",
                 "url":  "https://github.com/comfyanonymous/ComfyUI",
                 "path": "."}]


# ── Platform helpers ────────────────────────────────────────────────────────────

def _internet(timeout: int = 5) -> bool:
    """Return True if any of the probe targets is reachable.

    Tries github.com:443 first (the actual download target), then falls back
    to two public DNS resolvers on port 53.  Port 53 is often blocked by
    firewalls even when general internet is available, so github.com:443 is
    the most reliable signal for whether a git clone will succeed.
    """
    probes = [
        ("github.com", 443),
        ("8.8.8.8", 53),
        ("1.1.1.1", 53),
    ]
    for host, port in probes:
        try:
            socket.setdefaulttimeout(timeout)
            socket.create_connection((host, port))
            return True
        except OSError:
            continue
    return False


def _run(cmd: list[str], cwd: str | None = None, stream: bool = False) -> int:
    if stream:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert proc.stdout
        try:
            for line in proc.stdout:
                s = line.rstrip()
                if s:
                    console.print(f"  [dim]{s[:120]}[/dim]")
        except KeyboardInterrupt:
            proc.kill()
            proc.wait()
            raise
        proc.wait()
        return proc.returncode
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"  [dim]{(r.stdout + r.stderr).strip()[:200]}[/dim]")
    return r.returncode


def _cleanup_partial(dest: Path) -> None:
    """Remove a partially-cloned directory, best-effort."""
    try:
        shutil.rmtree(dest)
        console.print(f"  [dim]Removed partial directory: {dest}[/dim]")
    except Exception as e:
        console.print(f"  [yellow]Could not remove {dest}: {e}[/yellow]")
        console.print(f"  [yellow]Delete it manually before retrying.[/yellow]")


def _read_input(prompt: str) -> "str | object":
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if os.name == "nt":
        import msvcrt, time as _t
        buf: list[str] = []
        while True:
            ch = msvcrt.getch()
            if ch == b"\x1b":
                _t.sleep(0.05)
                while msvcrt.kbhit():
                    msvcrt.getch()
                sys.stdout.write("\n"); sys.stdout.flush()
                return _ESC
            if ch in (b"\r", b"\n"):
                sys.stdout.write("\n"); sys.stdout.flush()
                return "".join(buf).strip()
            if ch in (b"\b", b"\x7f"):
                if buf:
                    buf.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
            elif ch in (b"\x00", b"\xe0"):
                msvcrt.getch()
            elif ch == b"\x03":
                raise KeyboardInterrupt
            else:
                try:
                    c = ch.decode("utf-8")
                    if c >= " ":
                        buf.append(c); sys.stdout.write(c); sys.stdout.flush()
                except Exception:
                    pass
    else:
        import tty, termios, select as _sel
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        buf2: list[str] = []
        try:
            tty.setraw(fd)
            while True:
                ch2 = sys.stdin.read(1)
                if ch2 == "\x1b":
                    ready, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                    if ready:
                        ch3 = sys.stdin.read(1)
                        if ch3 == "[" and _sel.select([sys.stdin], [], [], 0.05)[0]:
                            sys.stdin.read(1)
                    else:
                        sys.stdout.write("\r\n"); sys.stdout.flush()
                        return _ESC
                elif ch2 in ("\r", "\n"):
                    sys.stdout.write("\r\n"); sys.stdout.flush()
                    return "".join(buf2).strip()
                elif ch2 in ("\b", "\x7f"):
                    if buf2:
                        buf2.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
                elif ch2 == "\x03":
                    raise KeyboardInterrupt
                elif ch2 >= " ":
                    buf2.append(ch2); sys.stdout.write(ch2); sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _ask(prompt: str, valid: tuple[str, ...] = ("y", "n"), default: str = "n") -> "str | object":
    while True:
        try:
            ans = _read_input(f"  {prompt} ")
        except (EOFError, KeyboardInterrupt):
            return default
        if ans is _ESC:
            return _ESC
        assert isinstance(ans, str)
        ans = ans.lower() or default
        if ans in valid:
            return ans
        console.print(f"  [yellow]Please enter one of: {', '.join(valid)}[/yellow]")


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    console.print("\n[bold]ComfyUI Installer[/bold]")
    console.print("[dim]Phase 1 — clone + venv  (small download)[/dim]")
    console.print("[dim]Phase 2 — full packages (large download, optional)[/dim]")
    console.print("[dim]Press Esc at any prompt to cancel and return to the menu.[/dim]\n")

    # ── Pre-flight ──────────────────────────────────────────────────────────────
    console.print("Checking requirements...")

    _vi = sys.version_info
    _ver_str = f"{_vi.major}.{_vi.minor}.{_vi.micro}"
    if _vi < (3, 9):
        console.print(
            f"[red][FAIL] Python {_ver_str} is too old.[/red]\n"
            f"  ComfyUI requires Python 3.9 or newer.\n"
            f"  Download Python 3.12 from https://python.org/downloads/"
        )
        sys.exit(1)
    elif _vi < (3, 10):
        console.print(
            f"[yellow][WARN] Python {_ver_str} — works, but 3.10+ is recommended "
            f"for best compatibility with ComfyUI and its extensions.[/yellow]"
        )
    else:
        console.print(f"[green][OK]  Python {_ver_str}[/green]")

    if not _internet():
        console.print("[red][FAIL] No internet connection — cannot proceed.[/red]")
        sys.exit(1)
    console.print("[green][OK]  Internet reachable[/green]")

    if not shutil.which("git"):
        console.print("[red][FAIL] git not found on PATH.[/red]")
        console.print("  Windows : https://git-scm.com/downloads")
        console.print("  Linux   : sudo apt install git  /  sudo dnf install git")
        console.print("  macOS   : brew install git  or  xcode-select --install")
        sys.exit(1)
    console.print("[green][OK]  git found[/green]")

    # ── Preset selection ────────────────────────────────────────────────────────
    presets_dir = Path(__file__).resolve().parent / "presets"
    repos = _pick_preset(presets_dir)
    if repos is None:
        console.print("\n[dim]Cancelled.[/dim]")
        return
    if not repos:
        console.print("\n[yellow]No repos in preset — nothing to install.[/yellow]")
        return

    # ── Destination ─────────────────────────────────────────────────────────────
    default_dest = str(Path.home() / "ComfyUI")
    console.print(f"\n[dim]Default install location: {default_dest}[/dim]")
    try:
        dest_raw = _read_input("  Install directory (Enter for default, Esc to cancel): ")
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        return
    if dest_raw is _ESC:
        console.print("[dim]Cancelled.[/dim]")
        return
    assert isinstance(dest_raw, str)
    dest = Path(dest_raw.strip()) if dest_raw.strip() else Path(default_dest)

    # ── Pre-existing directory handling ─────────────────────────────────────────
    skip_clone = False
    if dest.exists() and any(dest.iterdir()):
        has_git    = (dest / ".git").exists()
        has_main   = (dest / "main.py").exists()
        has_venv   = (dest / ".venv").exists()

        if has_git and not has_main:
            # .git present but working tree missing → interrupted before checkout
            console.print(
                f"[yellow][WARN] {dest} contains an incomplete git clone "
                f"(probably from a previous interrupted install).[/yellow]"
            )
            ans = _ask("Delete it and start fresh? (y/N):", default="n")
            if ans is _ESC or ans != "y":
                console.print("[dim]Cancelled.[/dim]")
                return
            _cleanup_partial(dest)

        elif has_git and has_main:
            # Clone completed previously.  Re-cloning into a non-empty directory
            # would fail immediately.  Offer to resume from the failed step instead.
            venv_label = "[green]present[/green]" if has_venv else "[yellow]missing[/yellow]"
            console.print(
                f"\n[yellow][WARN] {dest} already contains a ComfyUI clone.[/yellow]"
            )
            console.print(f"  .venv : {venv_label}")
            console.print()
            console.print("  [bold cyan]R[/bold cyan]  Resume — skip clone, redo venv + package steps")
            console.print("  [bold cyan]D[/bold cyan]  Delete and reinstall from scratch")
            console.print("  [bold cyan]N[/bold cyan]  Cancel")
            console.print()
            while True:
                ans = _read_input("  Choice (R/D/N, default N): ")
                if ans is _ESC:
                    ans = "n"
                assert isinstance(ans, str)
                ans = ans.strip().lower() or "n"
                if ans in ("r", "d", "n"):
                    break
                console.print("  [yellow]Enter R, D, or N.[/yellow]")

            if ans == "n":
                console.print("[dim]Cancelled.[/dim]")
                return
            if ans == "d":
                _cleanup_partial(dest)
            else:  # resume
                skip_clone = True
                console.print("[dim]Resuming from venv/package step...[/dim]")

        else:
            # No .git at all — directory has unrelated files
            console.print(f"[yellow][WARN] {dest} already exists and is not empty.[/yellow]")
            ans = _ask("Continue anyway? (y/N):", default="n")
            if ans is _ESC or ans != "y":
                console.print("[dim]Cancelled.[/dim]")
                return

    dest.parent.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: clone all repos ─────────────────────────────────────────────────
    if skip_clone:
        console.print(f"\n[dim]Clone step skipped — using existing {dest}[/dim]\n")
        base_cloned = True
    else:
        console.print(f"\n[bold]Cloning {len(repos)} repo(s) into {dest}...[/bold]\n")
        base_cloned = False

    try:
        for repo in repos:
            name      = repo.get("name", repo.get("url", "?").split("/")[-1])
            url       = repo.get("url", "")
            rel_path  = repo.get("path", ".")

            if not url:
                console.print(f"  [yellow]Skipping {name} — no url defined.[/yellow]")
                continue

            # Skip the base clone when resuming; still clone any sub-repos that
            # may be listed in the preset (custom nodes, etc.) as long as their
            # destination directory doesn't already exist.
            if skip_clone and rel_path == ".":
                continue

            repo_dest = dest if rel_path == "." else dest / rel_path

            if skip_clone and repo_dest.exists() and any(repo_dest.iterdir()):
                console.print(f"  [dim]Skipping {name} — {repo_dest} already exists[/dim]")
                continue

            if repo_dest != dest:
                repo_dest.parent.mkdir(parents=True, exist_ok=True)

            console.print(f"  [bold]Cloning {name}[/bold]  [dim]{url}[/dim]")
            rc = _run(["git", "clone", "--depth=1", url, str(repo_dest)], stream=True)
            if rc != 0:
                if rel_path == ".":
                    console.print(f"[red][FAIL] Could not clone {name}.[/red]")
                    console.print(f"[dim]Cleaning up partial directory...[/dim]")
                    _cleanup_partial(dest)
                    sys.exit(1)
                else:
                    console.print(f"  [yellow][WARN] Could not clone {name} — continuing without it.[/yellow]")
                    continue

            console.print(f"  [green][OK]  {name} cloned[/green]\n")
            if rel_path == ".":
                base_cloned = True

    except KeyboardInterrupt:
        console.print("\n[yellow]Install cancelled.[/yellow]")
        if not base_cloned and dest.exists():
            console.print("[dim]Cleaning up partial directory...[/dim]")
            _cleanup_partial(dest)
        sys.exit(1)

    if not base_cloned:
        console.print("[red][FAIL] ComfyUI base was not cloned successfully.[/red]")
        _cleanup_partial(dest)
        sys.exit(1)

    # ── Phase 1: venv ───────────────────────────────────────────────────────────
    console.print("[bold]Creating virtual environment...[/bold]")
    venv_path  = dest / ".venv"
    pip_python = sys.executable
    if _run([sys.executable, "-m", "venv", str(venv_path)]) != 0:
        console.print(
            "[red][FAIL] Could not create virtual environment.[/red]\n"
            "  Common causes:\n"
            "    • python3-venv is not installed (Linux: sudo apt install python3-venv)\n"
            "    • The destination path has a space or special character\n"
            "    • Insufficient disk space\n"
            "  Continuing with the current Python — packages will be installed globally.\n"
            "  [yellow]Warning: this may conflict with other projects.[/yellow]"
        )
    else:
        console.print("[green][OK]  Virtual environment created (.venv)[/green]")
        pip_python = str(
            venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )

    console.print("\n[bold green]Phase 1 complete.[/bold green]")
    console.print("  ComfyUI is cloned and the venv is ready.")
    console.print("  [dim]Models / checkpoints are not included — add them separately.[/dim]")

    # ── Phase 2 (optional) ──────────────────────────────────────────────────────
    console.print("\n[bold]Phase 2 — full package install[/bold]")
    console.print(
        "  Installs everything in requirements.txt, including [bold]PyTorch[/bold].\n"
        "  Download size is roughly [bold]2–4 GB[/bold] depending on GPU backend.\n"
        "  You can skip now and run it later:\n"
        "  [dim]pip install -r requirements.txt[/dim]\n"
    )

    if not _internet():
        console.print("[yellow][WARN] Internet no longer reachable — skipping Phase 2.[/yellow]")
        _finish(dest)
        return

    ans = _ask("Proceed with the large download now? (y/N):", default="n")
    if ans is _ESC or ans != "y":
        console.print("[dim]Skipped. Run pip install manually when ready.[/dim]")
        _finish(dest)
        return

    # GPU backend picker
    console.print("\n[bold]Select your GPU / backend:[/bold]")
    for key, label, _ in _TORCH_BACKENDS:
        console.print(f"  {key}  {label}")
    console.print()

    try:
        gpu_choice = _read_input("  Enter number (default 1): ")
    except (EOFError, KeyboardInterrupt):
        gpu_choice = "5"
    if gpu_choice is _ESC:
        gpu_choice = "5"
    assert isinstance(gpu_choice, str)
    gpu_choice = gpu_choice.strip() or "1"

    backend_label, index_url = next(
        ((lbl, u) for k, lbl, u in _TORCH_BACKENDS if k == gpu_choice),
        (_TORCH_BACKENDS[-1][1], _TORCH_BACKENDS[-1][2]),
    )

    if index_url is None:
        console.print("[dim]PyTorch install skipped.[/dim]")
    else:
        req_file = dest / "requirements.txt"
        if req_file.exists():
            console.print(f"\n[bold]Installing requirements ({backend_label})...[/bold]")
            pip_cmd = [pip_python, "-m", "pip", "install", "-r", str(req_file)]
            if index_url:
                pip_cmd += ["--extra-index-url", index_url]
            if _run(pip_cmd, stream=True) != 0:
                console.print(
                    "[yellow][WARN] Some packages failed.\n"
                    "  You may need to install PyTorch manually.[/yellow]\n"
                    "  [cyan]https://pytorch.org/get-started/locally/[/cyan]"
                )
            else:
                console.print("[green][OK]  Packages installed[/green]")
        else:
            console.print("[yellow][WARN] requirements.txt not found — skipping.[/yellow]")

    _finish(dest)


def _finish(dest: Path) -> None:
    pip_python = str(
        (dest / ".venv") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    console.print(f"\n[bold green]Done.[/bold green]  ComfyUI is at: [cyan]{dest}[/cyan]")
    console.print(f"  Start:  [dim]{pip_python} main.py[/dim]")
    console.print("  Then press [bold]R[/bold] in this menu to re-detect it.\n")
    print(dest, flush=True)


if __name__ == "__main__":
    main()
