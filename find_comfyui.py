"""
Standalone ComfyUI discovery helper — called by comfyui-health.bat / .sh / .command

Detection strategy
──────────────────
Rather than relying on folder names, we score each candidate directory against
ComfyUI's structural fingerprint.  A score ≥ 60 = auto-accept; 20-59 = ask the
user to confirm; < 20 = skip silently.

stdout : the chosen ComfyUI path (captured by launcher into COMFYUI_PATH)
stderr : all user-visible messages and confirmation prompts
exit 0 : path written to stdout
exit 1 : nothing found / user declined all candidates
"""
from __future__ import annotations

import os
import sys
import platform
from pathlib import Path
from typing import NamedTuple


# ── Structural fingerprint ────────────────────────────────────────────────────

class _Sig(NamedTuple):
    path_parts: tuple[str, ...]   # relative path components to test
    score: int
    is_file: bool = False


# Each entry: what to look for, how many points it's worth.
_SIGNATURES: list[_Sig] = [
    # "comfy/" alone is not unique — the PyPI "comfy" package exists and other
    # projects use that name.  Pair the directory with a source file that has
    # been present since ComfyUI's first commit and appears nowhere else.
    _Sig(("comfy",),                              20),     # subdir named 'comfy'
    _Sig(("comfy", "model_management.py"),        30, True),  # uniquely ComfyUI
    _Sig(("custom_nodes",),                       30),     # nearly always present
    _Sig(("comfy_extras",),                       20),     # extra node packs folder
    _Sig(("main.py",),                     15, True),
    _Sig(("server.py",),                   12, True),
    _Sig(("web",),                         10),
    _Sig(("web", "index.html"),            10, True),
    _Sig(("nodes",),                        8),
    _Sig(("models",),                       8),
    _Sig(("models", "checkpoints"),         8),
    _Sig(("models", "loras"),               6),
    _Sig(("models", "vae"),                 5),
    _Sig(("input",),                        4),
    _Sig(("output",),                       4),
    _Sig(("requirements.txt",),             3, True),
]

_AUTO_THRESHOLD    = 60   # ≥ this → accept silently
_CONFIRM_THRESHOLD = 20   # ≥ this (but < AUTO) → ask user
# below CONFIRM_THRESHOLD → skip

# ComfyUI ports to check for a running instance
_COMFYUI_PORTS = (8188, 8189, 7860, 7861, 8080, 8000, 3000, 3001)


def _score(d: Path) -> int:
    """Return a 0-100 confidence score that d is a ComfyUI root."""
    if not d.is_dir():
        return 0
    total = 0
    for sig in _SIGNATURES:
        p = d.joinpath(*sig.path_parts)
        hit = p.is_file() if sig.is_file else p.is_dir()
        if hit:
            total += sig.score
    return min(total, 100)


def _is_comfyui_strict(d: Path) -> bool:
    """Legacy strict check: has main.py or server.py (for known-good paths)."""
    return d.is_dir() and ((d / "main.py").exists() or (d / "server.py").exists())


def _name_looks_custom(name: str) -> bool:
    """True if the folder name doesn't obviously say 'comfyui'."""
    return "comfyui" not in name.lower()


# ── Active instance detection ─────────────────────────────────────────────────

def _active_comfyui() -> Path | None:
    """Find a running ComfyUI by checking ports, then Python process cwds."""
    try:
        import psutil

        # 1. Listening sockets on known ComfyUI ports
        listening: dict[int, int] = {}   # port → pid
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.pid:
                    listening[getattr(c.laddr, "port", 0)] = c.pid
        except (psutil.AccessDenied, PermissionError):
            pass  # no root on Linux/Mac — fall through to process scan

        for port in _COMFYUI_PORTS:
            pid = listening.get(port)
            if pid:
                try:
                    cwd = Path(psutil.Process(pid).cwd())
                    # Port 8188 is strongly associated with ComfyUI but not
                    # exclusive — require a real structural score before trusting it.
                    if _score(cwd) >= _AUTO_THRESHOLD:
                        return cwd
                except Exception:
                    pass

        # 2. Any Python process whose cwd looks like ComfyUI
        for proc in psutil.process_iter(["pid", "name", "cwd", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cwd = proc.info.get("cwd")
                if not cwd:
                    continue
                cwd = Path(cwd)
                if _score(cwd) >= _AUTO_THRESHOLD:
                    return cwd
                # Also check if cmdline mentions main.py inside a scored dir
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "main.py" in cmdline:
                    for part in (proc.info.get("cmdline") or []):
                        if part.endswith("main.py"):
                            candidate = Path(part).parent
                            if _score(candidate) >= _CONFIRM_THRESHOLD:
                                return candidate
            except Exception:
                pass

    except ImportError:
        pass
    return None


# ── Broad filesystem scan ─────────────────────────────────────────────────────

def _scan_parent(base: Path, results: dict[Path, int], depth: int = 1) -> None:
    """
    Scan `base` up to `depth` levels.  Any subdir scoring >= _CONFIRM_THRESHOLD
    is added to `results` with its score.
    """
    if not base.is_dir():
        return
    try:
        for d in base.iterdir():
            if not d.is_dir():
                continue
            s = _score(d)
            if s >= _CONFIRM_THRESHOLD:
                resolved = d.resolve()
                if resolved not in results:
                    results[resolved] = s
            # One level of recursion for portable zip layout
            if depth > 0:
                _scan_parent(d, results, depth - 1)
    except (PermissionError, OSError):
        pass


def _candidate_parents() -> list[Path]:
    """Directories to scan one level deep."""
    home = Path.home()
    parents: list[Path] = [
        home,
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "ai",
        home / "AI",
        home / "stable-diffusion",
        home / "sd",
        home / "Projects",
        home / "projects",
        home / "dev",
    ]

    # Windows drive roots + common sub-dirs
    if platform.system() == "Windows":
        for drive in ("C:", "D:", "E:", "F:", "G:"):
            root = Path(f"{drive}/")
            if root.exists():
                parents.append(root)
            for sub in ("ai", "AI", "SD", "stable-diffusion", "tools", "apps", "dev"):
                p = root / sub
                if p.exists():
                    parents.append(p)

    # Linux / macOS
    if platform.system() in ("Linux", "Darwin"):
        for p in (
            Path("/opt"),
            Path("/workspace"),    # RunPod / Vast.ai
            Path("/vol"),          # RunPod network volume
            Path("/content"),      # Google Colab
            Path("/tmp"),
        ):
            if p.exists():
                parents.append(p)

    # macOS extra
    if platform.system() == "Darwin":
        for p in (
            home / "Applications",
            Path("/Applications"),
        ):
            if p.exists():
                parents.append(p)

    # APPDATA / LOCALAPPDATA (Windows Desktop app)
    for var in ("APPDATA", "LOCALAPPDATA"):
        val = os.environ.get(var)
        if val:
            p = Path(val) / "Programs"
            if p.exists():
                parents.append(p)
            parents.append(Path(val))

    return parents


def _find_all_scored() -> dict[Path, int]:
    """
    Return {resolved_path: score} for every plausible ComfyUI directory,
    scanning broadly via structural fingerprinting.
    """
    results: dict[Path, int] = {}

    # Scan all candidate parents
    for parent in _candidate_parents():
        _scan_parent(parent, results, depth=1)

    # Also honour COMFYUI_PATH env var if set
    env = os.environ.get("COMFYUI_PATH")
    if env:
        p = Path(env)
        s = _score(p)
        if s > 0:
            results[p.resolve()] = max(results.get(p.resolve(), 0), s)

    return results


# ── Confirmation prompt ───────────────────────────────────────────────────────

def _ask_confirm(d: Path, score: int, why: list[str]) -> bool:
    """
    Ask the user whether d looks like their ComfyUI install.
    `why` is a list of structural clues we found.
    Returns True if they confirm.
    """
    print("", file=sys.stderr)
    print(f"  [?] Found a directory that looks like ComfyUI (confidence {score}/100):", file=sys.stderr)
    print(f"      {d}", file=sys.stderr)
    if why:
        print(f"      Detected: {', '.join(why)}", file=sys.stderr)
    if _name_looks_custom(d.name):
        print(f"      (Folder is not named 'ComfyUI' — confirming with you)", file=sys.stderr)
    print("", file=sys.stderr)
    try:
        ans = input("  Is this your ComfyUI installation? [y/n/s=skip all]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "1")


def _why_clues(d: Path) -> list[str]:
    """Return human-readable list of what we found inside d."""
    clues = []
    checks = [
        ("comfy/",            d / "comfy"),
        ("custom_nodes/",     d / "custom_nodes"),
        ("comfy_extras/",     d / "comfy_extras"),
        ("main.py",           d / "main.py"),
        ("server.py",         d / "server.py"),
        ("models/",           d / "models"),
        ("web/",              d / "web"),
        ("nodes/",            d / "nodes"),
        ("input/ + output/",  None),
    ]
    for label, path in checks[:-1]:
        if path and path.exists():
            clues.append(label)
    if (d / "input").is_dir() and (d / "output").is_dir():
        clues.append("input/ + output/")
    return clues


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Prefer any currently-running instance first
    active = _active_comfyui()

    scored = _find_all_scored()

    if not scored and active is None:
        print("[WARN] No ComfyUI installation found by structural scan.", file=sys.stderr)
        print("       Tip: make sure ComfyUI's folder contains main.py + a 'comfy/' subfolder.", file=sys.stderr)
        sys.exit(1)

    # Sort by score descending; bump running instance to top
    ranked: list[tuple[int, Path]] = sorted(
        ((s, p) for p, s in scored.items()),
        key=lambda x: (-(x[0] + (100 if active and x[1] == active.resolve() else 0)), str(x[1])),
    )

    # ── Single unambiguous result ──────────────────────────────────────────────
    high_confidence = [(s, p) for s, p in ranked if s >= _AUTO_THRESHOLD]
    if len(high_confidence) == 1:
        s, p = high_confidence[0]
        label = "active" if active and p == active.resolve() else f"score {s}/100"
        print(f"[OK] ComfyUI ({label}): {p}", file=sys.stderr)
        print(p)
        sys.exit(0)

    # Running instance always wins if we can identify it
    if active:
        active_r = active.resolve()
        for s, p in ranked:
            if p == active_r:
                print(f"[OK] ComfyUI (running): {p}", file=sys.stderr)
                print(p)
                sys.exit(0)
        # Active process found but not in scan (very custom path).
        # Require AUTO_THRESHOLD here: a low-scoring cwd (e.g. a Flask dev server
        # running on port 8188 from a generic ML project) must not be silently
        # accepted.  The interactive menu's manual-path prompt is the fallback.
        active_score = _score(active)
        if active_score >= _AUTO_THRESHOLD:
            print(f"[OK] ComfyUI (running, custom path): {active}", file=sys.stderr)
            print(active)
            sys.exit(0)

    # ── Multiple high-confidence results → auto-select highest scored ─────────
    # The interactive menu (interactive_menu.py) shows a proper arrow-key picker
    # at startup when it detects multiple installations, so no prompt is needed
    # here.  Prompting here would also corrupt the bat's stdout-capture of the
    # path (input() writes its prompt to stdout, not stderr).
    if len(high_confidence) > 1:
        print(f"\n[INFO] Found {len(high_confidence)} ComfyUI installations:", file=sys.stderr)
        for i, (s, p) in enumerate(high_confidence, 1):
            label = " ← running" if active and p == active.resolve() else ""
            print(f"  {i})  {p}  [{s}/100]{label}", file=sys.stderr)
        s, p = high_confidence[0]
        print(f"[INFO] Auto-selecting (1): {p}  — use the menu to switch", file=sys.stderr)
        print(p)
        sys.exit(0)

    # ── Medium-confidence candidates — ask user one at a time ─────────────────
    medium = [(s, p) for s, p in ranked if _CONFIRM_THRESHOLD <= s < _AUTO_THRESHOLD]

    if not medium and not high_confidence:
        print("[WARN] No ComfyUI installation found with sufficient confidence.", file=sys.stderr)
        sys.exit(1)

    # If there are any high_confidence at all (just zero-selected above), use first
    if high_confidence:
        s, p = high_confidence[0]
        print(f"[OK] ComfyUI (score {s}/100): {p}", file=sys.stderr)
        print(p)
        sys.exit(0)

    # Walk through medium-confidence, asking the user
    print(
        "\n[INFO] No obvious ComfyUI folder found by name, but I found directories\n"
        "       that structurally match ComfyUI. Checking with you one by one...",
        file=sys.stderr,
    )
    for s, p in medium:
        clues = _why_clues(p)
        if _ask_confirm(p, s, clues):
            print(p)
            sys.exit(0)

    # User declined everything
    print("\n[WARN] No ComfyUI installation confirmed.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
