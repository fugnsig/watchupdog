"""Rich terminal dashboard renderer."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .models import (
    FullHealthReport,
    GenerationStats,
    HealthCheckResult,
    HealthStatus,
    JobStatus,
    NunchakuInfo,
    QueueStats,
    SystemStats,
)

console = Console(highlight=False)

_GiB = 1024 ** 3

_STATUS_COLORS = {
    HealthStatus.OK: "bold green",
    HealthStatus.WARN: "bold yellow",
    HealthStatus.CRITICAL: "bold red",
    HealthStatus.UNKNOWN: "dim",
}

_JOB_STATUS_ICONS = {
    JobStatus.SUCCESS: "[green]OK[/green]",
    JobStatus.ERROR: "[red]ERR[/red]",
    JobStatus.INTERRUPTED: "[yellow]INT[/yellow]",
    JobStatus.UNKNOWN: "[dim]?[/dim]",
}


def _status_badge(status: HealthStatus) -> Text:
    labels = {
        HealthStatus.OK: ("[ OK ]", "bold green"),
        HealthStatus.WARN: ("[WARN]", "bold yellow"),
        HealthStatus.CRITICAL: ("[CRIT]", "bold red on dark_red"),
        HealthStatus.UNKNOWN: ("[ ?? ]", "dim"),
    }
    label, style = labels[status]
    return Text(label, style=style)


def _bytes_to_gb(b: int) -> str:
    return f"{b / (1024**3):.1f} GB"


def render_system_panel(
    stats: SystemStats | None,
    disk_warn_bytes: int = 20 * _GiB,
    disk_crit_bytes: int = 5 * _GiB,
) -> Panel:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column("Metric", style="dim")
    table.add_column("Value")

    if stats is None:
        table.add_row("Status", "[red]Unavailable[/red]")
        return Panel(table, title="[bold]System[/bold]", border_style="dim")

    cpu_color = "red" if stats.cpu_utilization > 90 else "yellow" if stats.cpu_utilization > 70 else "green"
    table.add_row("CPU", f"[{cpu_color}]{stats.cpu_utilization:.0f}%[/{cpu_color}]")

    ram_total = stats.ram_total
    ram_used = stats.ram_used
    if ram_total == 0:
        from .metrics import get_ram_info
        local = get_ram_info()
        if local:
            ram_total = local["total"]
            ram_used = local["used"]
    if ram_total > 0:
        ram_pct = (ram_used / ram_total) * 100
        ram_color = "red" if ram_pct > 85 else "yellow" if ram_pct > 70 else "green"
        table.add_row(
            "RAM",
            f"[{ram_color}]{_bytes_to_gb(ram_used)} / {_bytes_to_gb(ram_total)} ({ram_pct:.0f}%)[/{ram_color}]",
        )

    gpu_devices = [d for d in stats.devices if d.type != "cpu"]
    # Pre-compute whether any two GPUs share the same name so we can disambiguate.
    _gpu_names = [d.name for d in gpu_devices]
    _name_is_unique = {n: _gpu_names.count(n) == 1 for n in _gpu_names}
    for idx, dev in enumerate(gpu_devices):
        vram_used = dev.vram_total - dev.vram_free
        if len(gpu_devices) == 1:
            vram_label = "VRAM"
        elif _name_is_unique[dev.name]:
            vram_label = dev.name
        else:
            vram_label = f"{dev.name} [{dev.index}]"
        if dev.vram_total > 0:
            vram_pct = (vram_used / dev.vram_total) * 100
            vram_color = "red" if vram_pct > 97 else "yellow" if vram_pct > 90 else "green"
            table.add_row(
                vram_label,
                f"[{vram_color}]{_bytes_to_gb(vram_used)} / {_bytes_to_gb(dev.vram_total)} ({vram_pct:.0f}%)[/{vram_color}]",
            )
        else:
            table.add_row("GPU", f"[dim]{dev.name}[/dim]")

    if stats.disk_total_bytes > 0:
        disk_free  = stats.disk_free_bytes
        disk_total = stats.disk_total_bytes
        disk_used  = disk_total - disk_free
        disk_pct   = (disk_used / disk_total) * 100
        disk_color = "red" if disk_free < disk_crit_bytes else "yellow" if disk_free < disk_warn_bytes else "green"
        table.add_row(
            "Disk",
            f"[{disk_color}]{_bytes_to_gb(disk_used)} / {_bytes_to_gb(disk_total)} ({disk_pct:.0f}%)[/{disk_color}]",
        )

    return Panel(table, title="[bold]System[/bold]", border_style="cyan")


def render_queue_panel(stats: QueueStats | None) -> Panel:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column("Metric", style="dim")
    table.add_column("Value")

    if stats is None:
        table.add_row("Status", "[red]Unavailable[/red]")
        return Panel(table, title="[bold]Queue[/bold]", border_style="dim")

    run_color = "green" if stats.running_count == 0 else "cyan"
    pend_color = "green" if stats.pending_count == 0 else "yellow" if stats.pending_count < 10 else "red"

    table.add_row("Running", f"[{run_color}]{stats.running_count}[/{run_color}]")
    table.add_row("Pending", f"[{pend_color}]{stats.pending_count}[/{pend_color}]")

    return Panel(table, title="[bold]Queue[/bold]", border_style="cyan")


def render_nunchaku_panel(info: NunchakuInfo | None) -> Panel:
    """Compact horizontal Nunchaku status — one line across the full width."""
    if info is None or not info.nodes_found:
        return Panel(
            "[yellow]Nunchaku nodes not detected[/yellow]",
            title="[bold]Nunchaku[/bold]",
            border_style="dim",
        )

    line = Text()

    def _kv(key: str, val: str, sep: bool = True) -> None:
        if sep and line:
            line.append("   ")
        line.append(f"{key} ", style="dim")
        line.append_text(Text.from_markup(val))

    _kv("DiT",     "[green]yes[/green]" if info.dit_loader_present   else "[red]no[/red]",    sep=False)
    _kv("TextEnc", "[green]yes[/green]" if info.text_encoder_present else "[yellow]n/a[/yellow]")
    if info.lora_loader_present:
        _kv("LoRA", "[green]yes[/green]")

    if info.precision_mode:
        prec_color = "cyan" if info.precision_mode == "FP4" else "magenta"
        _kv("Precision", f"[{prec_color}]{info.precision_mode}[/{prec_color}]")

    fb_color = "green" if info.fb_cache_enabled else "dim"
    _kv("FB Cache", f"[{fb_color}]{'ON' if info.fb_cache_enabled else 'OFF'}[/{fb_color}]")

    if info.version:
        _kv("Version", info.version)

    return Panel(line, title="[bold]Nunchaku[/bold]", border_style="cyan")


def _time_ago(ms: float) -> str:
    import time
    delta = time.time() - ms / 1000
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h {int((delta % 3600) // 60)}m ago"
    return f"{int(delta // 86400)}d ago"


def render_stats_panel(stats: GenerationStats | None) -> Panel:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column("Metric", style="dim")
    table.add_column("Value")

    if stats is None or stats.total_jobs == 0:
        table.add_row("Activity", "[dim]No history yet[/dim]")
        return Panel(table, title="[bold]Activity[/bold]", border_style="dim")

    if stats.last_completed_ms:
        table.add_row("Last job", _time_ago(stats.last_completed_ms))

    table.add_row("Completed", f"[green]{stats.completed_jobs}[/green]")

    if stats.avg_exec_time_ms is not None:
        secs = stats.avg_exec_time_ms / 1000
        avg_str = f"{secs:.0f}s" if secs >= 1 else f"{stats.avg_exec_time_ms:.0f}ms"
        table.add_row("Avg time", avg_str)

    if stats.cancelled_jobs:
        table.add_row("Cancelled", f"[dim]{stats.cancelled_jobs}[/dim]")

    if stats.failed_jobs:
        err_color = "red" if stats.error_rate_pct > 20 else "yellow"
        table.add_row(
            "Errors",
            f"[{err_color}]{stats.failed_jobs}[/{err_color}]"
            f"[dim]  ({stats.error_rate_pct:.0f}%)[/dim]",
        )

    return Panel(table, title="[bold]Activity[/bold]", border_style="cyan")


def render_alerts_panel(alerts: list[str]) -> Panel:
    if not alerts:
        return Panel(
            "[green]No active alerts[/green]",
            title="[bold]Alerts[/bold]",
            border_style="green",
        )

    text = Text()
    for a in alerts:
        text.append("  * ", style="yellow")
        text.append(a + "\n")

    return Panel(text, title="[bold red]Alerts[/bold red]", border_style="red")


# Checks already represented by visual panels — no need to repeat them in the
# diagnostics strip.
_PANEL_CHECKS = {"vram", "ram", "queue", "error_rate", "nunchaku_nodes", "disk_space"}


def _render_diagnostics(checks: list[HealthCheckResult]) -> Table:
    """
    Compact table showing only checks *not* already visible in a panel.
    Each row is one line — name, coloured status badge, message.
    """
    _styles = {
        HealthStatus.OK:       ("green",  " OK "),
        HealthStatus.WARN:     ("yellow", "  ! "),
        HealthStatus.CRITICAL: ("red",    " !! "),
        HealthStatus.UNKNOWN:  ("dim",    "  ? "),
    }
    t = Table(box=None, show_header=False, expand=True, padding=(0, 1))
    t.add_column("badge", width=5, no_wrap=True)
    t.add_column("name",  style="dim", min_width=20, no_wrap=True)
    t.add_column("message")

    for c in checks:
        if c.name in _PANEL_CHECKS:
            continue
        colour, icon = _styles.get(c.status, ("dim", " ? "))
        display_name = c.name.replace("_", " ").title()
        t.add_row(
            f"[{colour}]{icon}[/{colour}]",
            display_name,
            c.message,
        )
    return t


def _three_col_grid(left: Any, mid: Any, right: Any) -> Table:
    """Return a 3-equal-column grid row using Table.grid (guaranteed equal widths)."""
    grid = Table.grid(expand=True, padding=(0, 0))
    grid.add_column(ratio=1)
    grid.add_column("gap", width=1)   # 1-char spacer between columns
    grid.add_column(ratio=1)
    grid.add_column("gap", width=1)
    grid.add_column(ratio=1)
    grid.add_row(left, "", mid, "", right)
    return grid


def build_report_renderable(
    report: FullHealthReport,
    disk_warn_bytes: int = 20 * _GiB,
    disk_crit_bytes: int = 5 * _GiB,
) -> Group:
    """Build the full report as a Rich renderable Group (for Live display)."""
    ts = report.timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Header
    badge = _status_badge(report.overall_status)
    header = Text()
    header.append("watchupdog  ", style="bold")
    header.append_text(badge)
    header.append(f"  {ts}", style="dim")
    header_panel = Panel(header, border_style=_STATUS_COLORS.get(report.overall_status, "dim"))

    # Top row: three equal columns — System | Queue | Activity
    top = _three_col_grid(
        render_system_panel(report.system_stats, disk_warn_bytes, disk_crit_bytes),
        render_queue_panel(report.queue_stats),
        render_stats_panel(report.generation_stats),
    )

    alerts = render_alerts_panel(report.alerts)
    diag   = _render_diagnostics(report.checks)

    parts: list[Any] = [header_panel, top]
    # Nunchaku: compact full-width strip below the main row (only if detected)
    if report.nunchaku and report.nunchaku.nodes_found:
        parts.append(render_nunchaku_panel(report.nunchaku))
    parts.append(alerts)
    # Only add diagnostics table if there are checks not already shown in a panel
    if any(c.name not in _PANEL_CHECKS for c in report.checks):
        parts.append(diag)

    return Group(*parts)


def build_report_renderable_live(
    report: FullHealthReport,
    interval: int = 5,
    next_in: float = 0.0,
    disk_warn_bytes: int = 20 * _GiB,
    disk_crit_bytes: int = 5 * _GiB,
) -> Group:
    """Like build_report_renderable but with a live refresh status footer."""
    ts = report.timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    next_secs = max(0, math.ceil(next_in))
    badge = _status_badge(report.overall_status)
    header = Text()
    header.append("watchupdog  ", style="bold")
    header.append_text(badge)
    header.append(f"  {ts}", style="dim")
    # Key hint lives in the header — always the first line rendered,
    # never clipped even on short terminals.
    header.append("   ·  refresh ", style="dim")
    header.append(f"{next_secs}s", style="dim cyan")
    header.append("  ·  press ", style="dim")
    header.append("Esc", style="bold cyan")
    header.append(" to exit", style="dim")
    header_panel = Panel(header, border_style=_STATUS_COLORS.get(report.overall_status, "dim"))

    top = _three_col_grid(
        render_system_panel(report.system_stats, disk_warn_bytes, disk_crit_bytes),
        render_queue_panel(report.queue_stats),
        render_stats_panel(report.generation_stats),
    )
    alerts = render_alerts_panel(report.alerts)
    diag   = _render_diagnostics(report.checks)

    parts: list[Any] = [header_panel, top]
    if report.nunchaku and report.nunchaku.nodes_found:
        parts.append(render_nunchaku_panel(report.nunchaku))
    parts.append(alerts)
    if any(c.name not in _PANEL_CHECKS for c in report.checks):
        parts.append(diag)

    return Group(*parts)


def render_full_report(
    report: FullHealthReport,
    disk_warn_bytes: int = 20 * _GiB,
    disk_crit_bytes: int = 5 * _GiB,
) -> None:
    """Print a complete one-shot report to the console."""
    console.print(build_report_renderable(report, disk_warn_bytes, disk_crit_bytes))
    console.print()  # ensure a blank line before the caller's "Press any key" prompt


def render_env_report(report: Any) -> None:  # EnvCheckReport from env_checks
    """Render the environment check table."""
    from .env_checks import STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_INFO

    # Group rows
    groups: dict[str, list] = {}
    for row in report.rows:
        groups.setdefault(row.group, []).append(row)

    for group_name, rows in groups.items():
        table = Table(
            title=group_name,
            box=box.SIMPLE_HEAD,
            expand=True,
            show_lines=False,
        )
        table.add_column("Check", min_width=35)
        table.add_column("Status", justify="center", min_width=12)
        table.add_column("Detail")

        status_render = {
            STATUS_OK:   "[bold green]OK[/bold green]",
            STATUS_WARN: "[bold yellow]WARN[/bold yellow]",
            STATUS_FAIL: "[bold red]FAIL[/bold red]",
            STATUS_INFO: "[bold blue]INFO[/bold blue]",
        }

        for row in rows:
            rendered_status = status_render.get(row.status, row.status)
            table.add_row(row.check, rendered_status, row.detail)

        console.print(table)

    # Summary
    console.print()
    summary_text = (
        f"[green]{report.passed} checks passed[/green], "
        f"[yellow]{report.warnings} warnings[/yellow], "
        f"[red]{report.failures} failures[/red]"
    )
    console.print(Panel(summary_text, title="Summary", border_style="dim"))

    if report.auto_fixed:
        console.print("\n[green]Auto-fixed:[/green]")
        for item in report.auto_fixed:
            console.print(f"  [green]>[/green] {item}")

    if report.manual_needed:
        # Deduplicate
        seen: set[str] = set()
        unique = [x for x in report.manual_needed if not (x in seen or seen.add(x))]  # type: ignore
        console.print("\n[yellow]Manual action needed:[/yellow]")
        for item in unique:
            console.print(f"  [yellow]->[/yellow] {item}")

    console.print()  # blank line before caller's "Press any key" prompt
