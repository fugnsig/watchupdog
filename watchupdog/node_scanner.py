"""Dynamic custom node scanner — reports all installed nodes generically."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CustomNodeInfo:
    name: str
    path: Path
    node_classes: list[str] = field(default_factory=list)
    has_requirements: bool = False
    requirement_count: int = 0
    has_install_script: bool = False


def scan_custom_nodes(comfyui_root: Path) -> list[CustomNodeInfo]:
    custom_nodes_dir = comfyui_root / "custom_nodes"
    if not custom_nodes_dir.exists():
        return []

    nodes: list[CustomNodeInfo] = []
    try:
        node_entries = sorted(custom_nodes_dir.iterdir())
    except (PermissionError, OSError):
        return nodes
    for d in node_entries:
        if not d.is_dir() or d.name.startswith("__") or d.name.startswith("."):
            continue

        req_path = d / "requirements.txt"
        has_req = req_path.exists()
        req_count = 0
        if has_req:
            try:
                lines = req_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                req_count = sum(1 for l in lines if l.strip() and not l.startswith("#"))
            except Exception:
                pass

        has_install = any((d / n).exists() for n in ("install.py", "setup.py", "install.bat"))
        node_classes = _extract_node_classes(d)

        nodes.append(CustomNodeInfo(
            name=d.name,
            path=d,
            node_classes=node_classes,
            has_requirements=has_req,
            requirement_count=req_count,
            has_install_script=has_install,
        ))

    return nodes


_NODE_CLASS_RE = re.compile(r'"([A-Za-z][A-Za-z0-9_\- ]+)"\s*:', re.MULTILINE)


def _extract_node_classes(node_dir: Path) -> list[str]:
    """Extract class names from NODE_CLASS_MAPPINGS in the node's source."""
    candidates = [
        node_dir / "__init__.py",
        node_dir / "nodes.py",
        node_dir / "nodes" / "__init__.py",
    ]
    classes: list[str] = []
    for f in candidates:
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            idx = text.find("NODE_CLASS_MAPPINGS")
            if idx == -1:
                continue
            chunk = text[idx: idx + 8000]
            classes.extend(_NODE_CLASS_RE.findall(chunk))
        except Exception:
            continue
    return sorted(set(classes))[:50]
