"""
Self-contained HTML health report export.

Generates a single .html file (no external dependencies) that can be
shared with others for debugging or reference.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import FullHealthReport, HealthStatus

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0f1117; color: #e0e0e0; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; line-height: 1.6; padding: 24px; }
h1 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
h2 { font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: #888; margin: 24px 0 10px; }
.banner { display: flex; align-items: center; gap: 16px; padding: 16px 20px; border-radius: 8px; margin-bottom: 24px; }
.banner.ok   { background: #0d2b1a; border: 1px solid #2ecc71; }
.banner.warn { background: #2b1f0a; border: 1px solid #f39c12; }
.banner.crit { background: #2b0d0d; border: 1px solid #e74c3c; }
.badge { font-size: 13px; font-weight: 700; padding: 4px 12px; border-radius: 4px; letter-spacing: 0.05em; }
.badge.ok   { background: #2ecc71; color: #000; }
.badge.warn { background: #f39c12; color: #000; }
.badge.crit { background: #e74c3c; color: #fff; }
.badge.info { background: #3498db; color: #fff; }
.meta { font-size: 12px; color: #888; }
table { width: 100%; border-collapse: collapse; margin-bottom: 8px; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: #666; padding: 6px 10px; border-bottom: 1px solid #222; }
td { padding: 7px 10px; border-bottom: 1px solid #1a1a1a; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #161b22; }
.ok-text   { color: #2ecc71; }
.warn-text { color: #f39c12; }
.crit-text { color: #e74c3c; }
.info-text { color: #3498db; }
.dim       { color: #555; font-size: 12px; }
.alerts { background: #1a0d0d; border: 1px solid #4a1a1a; border-radius: 6px; padding: 12px 16px; margin-bottom: 16px; }
.alerts li { margin: 4px 0 4px 16px; color: #e74c3c; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-card { background: #161b22; border: 1px solid #222; border-radius: 6px; padding: 12px 14px; }
.stat-card .label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.06em; }
.stat-card .value { font-size: 18px; font-weight: 700; margin-top: 2px; }
footer { margin-top: 32px; font-size: 11px; color: #444; border-top: 1px solid #1a1a1a; padding-top: 12px; }
"""

def _status_class(status: str) -> str:
    s = str(status).upper()
    if "OK" in s:    return "ok"
    if "WARN" in s:  return "warn"
    if "CRIT" in s:  return "crit"
    return "info"


def _status_badge(status: str) -> str:
    cls = _status_class(status)
    label = html.escape(str(status))
    return f'<span class="badge {cls}">{label}</span>'


def _esc(v: object) -> str:
    return html.escape(str(v) if v is not None else "")


def _section(title: str, body: str) -> str:
    return f'<h2>{_esc(title)}</h2>\n{body}\n'


def _checks_table(report: "FullHealthReport") -> str:
    rows = ""
    for c in report.checks:
        cls = _status_class(str(c.status))
        rows += (
            f"<tr>"
            f"<td>{_esc(c.name)}</td>"
            f'<td class="{cls}-text"><b>{_esc(c.status)}</b></td>'
            f"<td>{_esc(c.message)}</td>"
            f"</tr>\n"
        )
    return (
        "<table>"
        "<tr><th>Check</th><th>Status</th><th>Detail</th></tr>\n"
        + rows
        + "</table>"
    )


def _gb(b: int) -> str:
    return f"{b / 1024 ** 3:.1f}"


def _system_cards(report: "FullHealthReport") -> str:
    if not report.system_stats:
        return '<p class="dim">System stats unavailable (ComfyUI offline during export)</p>'

    s = report.system_stats
    cards = ""

    # VRAM per GPU device
    gpu_devices = [d for d in s.devices if d.type != "cpu"]
    for dev in gpu_devices:
        used  = dev.vram_total - dev.vram_free
        total = dev.vram_total
        pct   = (used / total * 100) if total else 0
        label = f"VRAM — {dev.name}" if len(gpu_devices) > 1 else "VRAM"
        cards += (
            f'<div class="stat-card">'
            f'<div class="label">{_esc(label)}</div>'
            f'<div class="value">{_gb(used)} / {_gb(total)} GB</div>'
            f'<div class="dim">{pct:.0f}% used</div>'
            f"</div>"
        )

    # RAM
    if s.ram_total:
        pct = (s.ram_used / s.ram_total * 100) if s.ram_total else 0
        cards += (
            f'<div class="stat-card">'
            f'<div class="label">RAM</div>'
            f'<div class="value">{_gb(s.ram_used)} / {_gb(s.ram_total)} GB</div>'
            f'<div class="dim">{pct:.0f}% used</div>'
            f"</div>"
        )

    # Disk
    if s.disk_total_bytes:
        free  = s.disk_free_bytes
        total = s.disk_total_bytes
        pct   = ((total - free) / total * 100) if total else 0
        cards += (
            f'<div class="stat-card">'
            f'<div class="label">Disk</div>'
            f'<div class="value">{_gb(free)} GB free</div>'
            f'<div class="dim">{_gb(total)} GB total &nbsp;({pct:.0f}% used)</div>'
            f"</div>"
        )

    # CPU
    if s.cpu_utilization:
        cards += (
            f'<div class="stat-card">'
            f'<div class="label">CPU</div>'
            f'<div class="value">{s.cpu_utilization:.0f}%</div>'
            f"</div>"
        )

    return f'<div class="grid">{cards}</div>'


def _queue_cards(report: "FullHealthReport") -> str:
    if not report.queue_stats:
        return ""
    q = report.queue_stats
    return (
        f'<div class="grid">'
        f'<div class="stat-card"><div class="label">Running</div><div class="value">{q.running_count}</div></div>'
        f'<div class="stat-card"><div class="label">Pending</div><div class="value">{q.pending_count}</div></div>'
        f"</div>"
    )


def _gen_stats_table(report: "FullHealthReport") -> str:
    g = report.generation_stats
    if not g:
        return ""
    rows = []
    avg_str = ""
    if g.avg_exec_time_ms is not None:
        secs = g.avg_exec_time_ms / 1000
        avg_str = f"{secs:.0f}s" if secs >= 1 else f"{g.avg_exec_time_ms:.0f}ms"

    pairs: list[tuple[str, Any]] = [
        ("Jobs analysed", g.total_jobs),
        ("Completed", g.completed_jobs),
        ("Cancelled", g.cancelled_jobs),
        ("Failed", g.failed_jobs),
        ("Error rate", f"{g.error_rate_pct:.1f}%"),
    ]
    if avg_str:
        pairs.append(("Avg job time", avg_str))
    for label, value in pairs:
        rows.append(f"<tr><td>{_esc(label)}</td><td><b>{_esc(value)}</b></td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def _nunchaku_section(report: "FullHealthReport") -> str:
    n = report.nunchaku
    if not n or not n.nodes_found:
        return ""
    rows = []
    if n.version:
        rows.append(f"<tr><td>Version</td><td>{_esc(n.version)}</td></tr>")
    if n.precision_mode:
        rows.append(f"<tr><td>Precision</td><td>{_esc(n.precision_mode)}</td></tr>")
    if n.fb_cache_enabled is not None:
        rows.append(f"<tr><td>FB Cache</td><td>{'enabled' if n.fb_cache_enabled else 'disabled'}</td></tr>")
    if n.nodes_found:
        node_list = ", ".join(n.nodes_found[:6])
        rows.append(f"<tr><td>Nodes</td><td>{_esc(node_list)}</td></tr>")
    return _section("Nunchaku", "<table>" + "".join(rows) + "</table>")


def _symlinks_section(report: "FullHealthReport") -> str:
    """Render a table of symlinked model folders if any were detected."""
    sym = next((c for c in report.checks if c.name == "symlinks"), None)
    if not sym:
        return ""
    entries = sym.details.get("symlinks", [])
    if not entries:
        return ""

    rows = ""
    for e in entries:
        if e["broken"]:
            status_cell = '<td class="warn-text"><b>BROKEN</b></td>'
        else:
            status_cell = '<td class="ok-text"><b>OK</b></td>'

        disk_str = ""
        if e.get("disk_free_gb") is not None:
            free  = e["disk_free_gb"]
            total = e.get("disk_total_gb") or 0
            disk_str = (
                f' <span class="dim">'
                f'({free:.1f} / {total:.1f} GB free'
                f'{"" if free >= 20 else " ⚠"})</span>'
            )

        cross_badge = ' <span class="dim" title="target is on a different drive">↗ cross-drive</span>' if e.get("cross_drive") else ""

        rows += (
            f"<tr>"
            f"<td>{_esc(e['link'])}</td>"
            f"{status_cell}"
            f"<td>{_esc(e['target'])}{cross_badge}{disk_str}</td>"
            f"</tr>\n"
        )

    table = (
        "<table>"
        "<tr><th>Link</th><th>Status</th><th>Target</th></tr>\n"
        + rows
        + "</table>"
    )
    return _section("Model Symlinks", table)


def export_html(
    report: "FullHealthReport",
    output_path: str | Path,
    comfyui_path: "Path | str | None" = None,
) -> Path:
    """Render a self-contained HTML health report and write it to output_path."""
    from .models import HealthStatus

    output_path = Path(output_path)
    status_str = report.overall_status.value if hasattr(report.overall_status, "value") else str(report.overall_status)
    banner_cls = _status_class(status_str)

    # Build identity lines
    build_name = Path(comfyui_path).name if comfyui_path else ""
    build_path = str(comfyui_path) if comfyui_path else ""

    meta_line = " &nbsp;|&nbsp; ".join(_esc(p) for p in [report.timestamp, report.comfyui_url] if p)

    build_line = ""
    if build_name or build_path:
        parts = []
        if build_name:
            parts.append(f'<span style="font-weight:700;color:#e0e0e0">{_esc(build_name)}</span>')
        if build_path:
            parts.append(f'<span style="color:#888">{_esc(build_path)}</span>')
        build_line = f'<div class="meta" style="margin-top:4px">Build: {" &nbsp;—&nbsp; ".join(parts)}</div>'

    # Alerts block
    alerts_html = ""
    if report.alerts:
        items = "".join(f"<li>{_esc(a)}</li>" for a in report.alerts)
        alerts_html = f'<div class="alerts"><ul>{items}</ul></div>'

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>watchupdog — {_esc(build_name or "Report")} — {_esc(status_str)}</title>
<style>{_CSS}</style>
</head>
<body>

<div class="banner {banner_cls}">
  <div>
    <h1>watchupdog report</h1>
    <div class="meta">{meta_line}</div>
    {build_line}
  </div>
  <div style="margin-left:auto">{_status_badge(status_str)}</div>
</div>

{alerts_html}

{_section("System", _system_cards(report))}
{_section("Queue", _queue_cards(report))}
{_section("Health Checks", _checks_table(report))}
{_symlinks_section(report)}
{_section("Generation Stats", _gen_stats_table(report))}
{_nunchaku_section(report)}

<footer>Generated by watchupdog &nbsp;|&nbsp; {_esc(report.timestamp)}</footer>
</body>
</html>"""

    try:
        output_path.write_text(body, encoding="utf-8")
    except (PermissionError, OSError) as e:
        raise PermissionError(
            f"Cannot write HTML report to {output_path}: {e}.  "
            f"Check that the directory exists and is writable."
        ) from e
    return output_path
