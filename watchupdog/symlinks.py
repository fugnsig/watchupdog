"""
Symlink scanner for ComfyUI model directories.

Many ComfyUI setups symlink model folders to keep large files on a
separate drive or share them across multiple installs.  This module
detects those symlinks, resolves their targets, and measures disk
space on any cross-drive targets so the health checks can surface
broken links and low-space warnings for model storage drives.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SymlinkEntry:
    link_path: Path         # absolute path of the symlink/junction
    link_rel: str           # display path relative to comfyui root
    target: Path            # resolved real path (may not exist if broken)
    is_broken: bool         # target does not exist or cannot be resolved
    is_dir: bool            # symlink points to a directory (or is a junction)
    cross_drive: bool       # target is on a different drive / mountpoint
    disk_free_gb: float | None = None    # free space on target drive (cross-drive only)
    disk_total_gb: float | None = None   # total space on target drive (cross-drive only)


def scan_model_symlinks(comfyui_path: str | Path) -> list[SymlinkEntry]:
    """
    Return all symlinks/junctions found inside <comfyui_path>/models/.

    Scanning strategy:
    - If models/ itself is a symlink, return that one entry and stop.
    - Otherwise scan one level deep (models/checkpoints/, models/loras/, …).
    - Does not recurse into subdirectories to keep the scan fast.

    Works on Windows (NTFS junctions + directory symlinks) and Unix.
    """
    root = Path(comfyui_path).resolve()
    models_dir = root / "models"

    install_drive = _mount_point(root)
    entries: list[SymlinkEntry] = []

    try:
        # Case 1: the entire models/ directory is itself a symlink/junction.
        # is_symlink() returns False (not raises) when the path doesn't exist.
        if models_dir.is_symlink():
            entries.append(_make_entry(models_dir, root, install_drive))
            return entries

        # Case 2: individual subdirectories are symlinked.
        # Materialise the iterator inside the try so any TOCTOU-race that removes
        # models_dir between the is_symlink() check and iterdir() raises OSError
        # here rather than propagating to the caller.
        for child in sorted(models_dir.iterdir()):
            if child.is_symlink():
                entries.append(_make_entry(child, root, install_drive))
    except (PermissionError, OSError):
        # Covers FileNotFoundError (models_dir removed mid-scan), PermissionError,
        # and any other OS-level race condition.
        pass

    return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_entry(
    link_path: Path,
    root: Path,
    install_drive: str,
) -> SymlinkEntry:
    """Build a SymlinkEntry for a single symlink, resolving target + disk."""

    # Relative display path (forward slashes for readability on all platforms)
    try:
        link_rel = link_path.relative_to(root).as_posix()
    except ValueError:
        link_rel = str(link_path)

    # Resolve the symlink target
    try:
        target = link_path.resolve()
        is_broken = not target.exists()
        # is_dir: True for dir junctions/symlinks regardless of broken status
        is_dir = link_path.is_dir()
    except (OSError, PermissionError):
        target = link_path   # best effort — use link path as placeholder
        is_broken = True
        is_dir = True

    cross_drive = False
    disk_free_gb: float | None = None
    disk_total_gb: float | None = None

    if not is_broken:
        try:
            target_drive = _mount_point(target)
            cross_drive = target_drive.lower() != install_drive.lower()
            if cross_drive:
                usage = shutil.disk_usage(target)
                disk_free_gb = usage.free / (1024 ** 3)
                disk_total_gb = usage.total / (1024 ** 3)
        except (OSError, PermissionError):
            pass

    return SymlinkEntry(
        link_path=link_path,
        link_rel=link_rel,
        target=target,
        is_broken=is_broken,
        is_dir=is_dir,
        cross_drive=cross_drive,
        disk_free_gb=disk_free_gb,
        disk_total_gb=disk_total_gb,
    )


def _mount_point(path: Path) -> str:
    """
    Return the drive letter (Windows) or mountpoint (Unix) for path.

    Windows:  C:\\  D:\\  \\\\server\\share  (anchor attribute)
    Unix:     /  /mnt/data  /home  (walk up until os.path.ismount)
    """
    if os.name == "nt":
        # anchor is reliable on Windows: 'C:\\', 'D:\\', or '\\\\server\\share\\'
        return str(path.anchor).upper()

    # Unix: walk parent chain until we hit a real mount point.
    p = path.resolve() if not path.is_absolute() else path
    while True:
        if os.path.ismount(str(p)):
            return str(p)
        parent = p.parent
        if parent == p:
            return str(p)  # root
        p = parent
