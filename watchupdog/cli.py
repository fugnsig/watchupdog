"""Click entry point for watchupdog CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import subprocess
import threading
import time

import click
from rich.live import Live
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .checks import (
    check_connectivity,
    check_disk_space,
    check_error_rate,
    check_model_files,
    check_nunchaku_nodes,
    check_queue_health,
    check_ram_health,
    check_stale_jobs,
    check_symlinks,
    check_vram_health,
    _parse_system_stats,
)
from .client import ComfyUIClient
from .config import load_config
from .dashboard import console, render_full_report, render_env_report
from .env_checks import run_env_checks
from .models import FullHealthReport, HealthStatus
from .nunchaku import detect_nunchaku

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _collect_report(
    url: str,
    config: Any,
    vram_warn: float,
    vram_crit: float,
    queue_warn: int,
    stale_minutes: float,
    history_jobs: int,
    running_since: dict[str, float] | None = None,
    comfyui_path: str | None = None,
) -> FullHealthReport:
    report = FullHealthReport(
        comfyui_url=url,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    async with ComfyUIClient(url, timeout=config.timeout) as client:
        raw = await client.fetch_all(history_jobs=history_jobs)

    # 1. Connectivity — if primary URL failed, try other ports automatically
    if raw["system_stats"] is None:
        from .client import probe_for_live_url
        alt_url = await probe_for_live_url(url, timeout=1.5)
        if alt_url:
            # Found ComfyUI on a different port — use it transparently
            report.alerts.append(
                f"Note: configured URL {url} didn't respond; "
                f"auto-switched to {alt_url} — update your settings to avoid this"
            )
            report.comfyui_url = alt_url
            async with ComfyUIClient(alt_url, timeout=config.timeout) as client2:
                raw = await client2.fetch_all(history_jobs=history_jobs)
            url = alt_url

    conn_check = check_connectivity(raw["system_stats"], url=url)
    report.checks.append(conn_check)
    if conn_check.status == HealthStatus.CRITICAL:
        report.overall_status = HealthStatus.CRITICAL
        report.alerts.append(conn_check.message)
        return report

    # 2. System stats
    report.system_stats = _parse_system_stats(raw["system_stats"])

    # 2b. Disk space — critical for AI setups with large model files
    disk_check, (disk_free, disk_total) = check_disk_space(
        comfyui_path=comfyui_path,
        warn_gb=config.thresholds.get("disk_warn_gb", 20.0),
        critical_gb=config.thresholds.get("disk_critical_gb", 5.0),
        warn_pct=config.thresholds.get("disk_warn_pct", 90.0),
        critical_pct=config.thresholds.get("disk_critical_pct", 95.0),
    )
    report.checks.append(disk_check)
    if report.system_stats and disk_total > 0:
        report.system_stats.disk_free_bytes = disk_free
        report.system_stats.disk_total_bytes = disk_total

    # 2c. Symlink awareness — detect symlinked model folders and cross-drive disk
    sym_check, _sym_entries = check_symlinks(
        comfyui_path=comfyui_path,
        warn_gb=config.thresholds.get("disk_warn_gb", 20.0),
        critical_gb=config.thresholds.get("disk_critical_gb", 5.0),
    )
    report.checks.append(sym_check)

    # 3. Nunchaku detection (precision mode comes from /object_info node inputs only)
    nunchaku = detect_nunchaku(raw["object_info"])
    report.nunchaku = nunchaku

    # 4. Queue health
    q_check, queue_stats = check_queue_health(raw["queue"], warn_threshold=queue_warn)
    report.checks.append(q_check)
    report.queue_stats = queue_stats

    # 5. Stale jobs
    stale_check = check_stale_jobs(raw["queue"], stale_minutes=stale_minutes, running_since=running_since)
    report.checks.append(stale_check)

    # 6. VRAM
    vram_check, _ = check_vram_health(
        raw["system_stats"],
        warn_pct=vram_warn,
        critical_pct=vram_crit,
        nunchaku=nunchaku,
        nunchaku_anomaly_gb=config.thresholds.get("nunchaku_vram_anomaly_gb", 14.0),
        nunchaku_min_card_gb=config.thresholds.get("nunchaku_min_card_gb", 12.0),
    )
    report.checks.append(vram_check)

    # 7. RAM
    ram_check = check_ram_health(
        raw["system_stats"],
        warn_pct=config.thresholds["ram_warn_pct"],
    )
    report.checks.append(ram_check)

    # 8. Nunchaku nodes — only relevant if nunchaku is loaded AND checks are enabled
    if nunchaku.nodes_found and config.get("nunchaku_checks", True):
        nunchaku_check = check_nunchaku_nodes(nunchaku)
        report.checks.append(nunchaku_check)

    # 9. Error rate + gen stats
    err_check, gen_stats = check_error_rate(raw["history"], history_jobs=history_jobs)
    report.checks.append(err_check)
    report.generation_stats = gen_stats

    # 10. Model files (via object_info)
    model_check = check_model_files(raw["object_info"], config.expected_models)
    report.checks.append(model_check)

    # Aggregate alerts
    for check in report.checks:
        if check.status in (HealthStatus.WARN, HealthStatus.CRITICAL):
            report.alerts.append(check.message)

    # Overall status
    statuses = [c.status for c in report.checks]
    if HealthStatus.CRITICAL in statuses:
        report.overall_status = HealthStatus.CRITICAL
    elif HealthStatus.WARN in statuses:
        report.overall_status = HealthStatus.WARN
    else:
        report.overall_status = HealthStatus.OK

    return report


# ---------------------------------------------------------------------------
# Install progress helper
# ---------------------------------------------------------------------------

def _run_fix_with_progress(label: str, cmd: str) -> bool:
    """Run a shell fix command, showing a spinner + live pip output, refreshing every 5s."""
    last_line: list[str] = ["starting..."]

    import shlex as _shlex
    cmd_list = _shlex.split(cmd) if isinstance(cmd, str) else cmd
    try:
        proc = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except (FileNotFoundError, OSError) as _exc:
        console.print(f"[red]  Cannot run fix command:[/red] {_exc}")
        return False

    def _reader() -> None:
        if proc.stdout is None:
            return
        for raw in proc.stdout:
            stripped = raw.strip()
            # Skip blank lines and pip notice lines
            if stripped and not stripped.startswith("[notice]"):
                last_line[0] = stripped

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            TextColumn("[dim]{task.fields[status]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task(label, total=None, status="starting...")
            last_update = time.time()

            while proc.poll() is None:
                time.sleep(0.1)
                if time.time() - last_update >= 5:
                    # Truncate long lines so they fit in the terminal
                    status = last_line[0][:70] if last_line[0] else "working..."
                    progress.update(task, status=status)
                    last_update = time.time()

            reader.join(timeout=5)
            rc = proc.returncode
            final = "[green]Done[/green]" if rc == 0 else f"[red]Failed (exit {rc})[/red]"
            progress.update(task, status=final)
    except BaseException:
        # Ctrl-C or any other interruption — kill the child so it doesn't
        # keep running as an orphan and corrupt the venv mid-install.
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        raise

    return rc == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command("watchupdog")
@click.option("--url", default=None, help="ComfyUI base URL (overrides config)")
@click.option("--watch", is_flag=True, help="Live dashboard mode")
@click.option("--interval", default=None, type=int, help="Refresh interval in seconds (default 5)")
@click.option("--json", "output_json", is_flag=True, help="Output JSON instead of rich UI")
@click.option("--alert-vram", default=None, type=float, help="VRAM warn threshold % (default 90)")
@click.option("--alert-vram-critical", default=None, type=float, help="VRAM critical threshold % (default 97)")
@click.option("--alert-queue", default=None, type=int, help="Queue depth warn threshold (default 10)")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to config TOML file")
@click.option("--env-check", "do_env_check", is_flag=True, help="Run environment audit (no ComfyUI needed)")
@click.option("--pip-check", "do_pip_check", is_flag=True, help="Run core package compatibility check only")
@click.option("--comfyui-path", "comfyui_path", default=None, type=click.Path(), help="Path to ComfyUI installation directory")
@click.option("--fix", is_flag=True, help="Auto-fix issues found by --env-check (snapshots first)")
@click.option("--backup", "do_backup", is_flag=True, help="Snapshot current pip state to backups/")
@click.option("--restore", "restore_path", default=None, type=click.Path(), help="Restore a pip snapshot (path or 'latest')")
@click.option("--dry-run", "dry_run", is_flag=True, help="With --restore / --restore-models: show what would change without applying it")
@click.option("--checksums", "do_checksums", is_flag=True, help="With --backup: compute SHA-256 checksums for all model files (slow)")
@click.option("--list-backups", "do_list_backups", is_flag=True, help="List available pip snapshots")
@click.option("--diff", "diff_target", default=None, is_eager=False, help="Diff two snapshots ('latest', path, or omit for latest two)")
@click.option("--html", "html_output", default=None, type=click.Path(), help="Export health report as self-contained HTML file")
@click.option("--settings", "do_settings", is_flag=True, help="Open interactive settings editor")
@click.option("--backup-workflows", "do_backup_workflows", is_flag=True, help="Snapshot all workflow JSON files to a zip archive")
@click.option("--list-workflow-backups", "do_list_workflow_backups", is_flag=True, help="List available workflow snapshot zips")
@click.option("--missing-models", "do_missing_models", is_flag=True, help="List models in latest snapshot that are no longer on disk")
@click.option("--restore-models", "restore_models_path", default=None, type=click.Path(), help="Re-download missing models from a snapshot (path or 'latest')")
def main(
    url: str | None,
    watch: bool,
    interval: int | None,
    output_json: bool,
    alert_vram: float | None,
    alert_vram_critical: float | None,
    alert_queue: int | None,
    config_path: str | None,
    do_env_check: bool,
    do_pip_check: bool,
    comfyui_path: str | None,
    fix: bool,
    do_backup: bool,
    restore_path: str | None,
    dry_run: bool,
    do_checksums: bool,
    do_list_backups: bool,
    diff_target: str | None,
    html_output: str | None,
    do_settings: bool,
    do_backup_workflows: bool,
    do_list_workflow_backups: bool,
    do_missing_models: bool,
    restore_models_path: str | None,
) -> None:
    """watchupdog — check the status of a running ComfyUI instance."""
    from pathlib import Path as _Path

    cfg = load_config(config_path)

    # COMFYUI_PATH env var or --comfyui-path flag both work
    _comfyui_root = (
        _Path(comfyui_path) if comfyui_path
        else _Path(os.environ["COMFYUI_PATH"]) if "COMFYUI_PATH" in os.environ
        else None
    )

    # --settings
    if do_settings:
        from .settings_editor import run_settings_editor
        run_settings_editor()
        sys.exit(0)


    # --diff
    if diff_target is not None or (len(sys.argv) > 1 and "--diff" in sys.argv):
        from .backup import diff_snapshots, list_snapshots
        from rich.table import Table
        with console.status("[dim]Comparing snapshots...[/dim]", spinner="dots"):
            snaps = list_snapshots()
            if len(snaps) < 2:
                console.print("[red]Need at least 2 snapshots to diff.[/red]")
                sys.exit(1)
            snap_b = snaps[0]
            if diff_target and diff_target not in ("latest", "previous", None):
                _match = next((s for s in snaps if s["file"] == diff_target), None)
                if _match is None:
                    snap_a = snaps[1]
                else:
                    snap_a = _match
            else:
                snap_a = snaps[1]
            diff = diff_snapshots(snap_a, snap_b)
        meta = diff["meta"]
        console.print(f"\n[bold]Snapshot diff[/bold]")

        def _snap_line(label: str, s: dict) -> None:
            ts    = s.get("timestamp", "?")
            note  = s.get("note", "")
            name  = s.get("comfyui_name", "")
            root  = s.get("comfyui_root", "")
            build = f"  [dim]└─ build:[/dim] [cyan]{name}[/cyan] [dim]{root}[/dim]" if name else ""
            console.print(f"  [dim]{label}[/dim] {ts}  {note}")
            if build:
                console.print(build)

        _snap_line("A (before):", meta["snapshot_a"])
        _snap_line("B (after): ", meta["snapshot_b"])

        pkgs = diff["packages"]
        console.print(f"\n[bold]Packages:[/bold]  {pkgs['summary']}")
        if pkgs["added"]:
            t = Table("Package", "Version", title="Added", title_style="green", show_header=True)
            for p in pkgs["added"]:
                t.add_row(p["name"], p["version"])
            console.print(t)
        if pkgs["removed"]:
            t = Table("Package", "Version", title="Removed", title_style="red", show_header=True)
            for p in pkgs["removed"]:
                t.add_row(p["name"], p["version"])
            console.print(t)
        if pkgs["changed"]:
            t = Table("Package", "From", "To", title="Changed", title_style="yellow", show_header=True)
            for p in pkgs["changed"]:
                t.add_row(p["name"], p["from"], p["to"])
            console.print(t)

        if diff["key_packages"]:
            t = Table("Key", "From", "To", title="Key Package Changes", title_style="cyan", show_header=True)
            for p in diff["key_packages"]:
                t.add_row(p["key"], p["from"], p["to"])
            console.print(t)

        if diff["custom_nodes"]:
            t = Table("Node", "From", "To", title="Custom Node Changes", title_style="cyan", show_header=True)
            for n in diff["custom_nodes"]:
                t.add_row(n["name"], n["from"], n["to"])
            console.print(t)

        if diff["models"]:
            m = diff["models"]
            fa = m["total_files"]["from"]
            fb = m["total_files"]["to"]
            console.print(f"\n[bold]Models:[/bold]  {fa} -> {fb} files")

        sys.exit(0)

    # --list-backups
    if do_list_backups:
        from pathlib import Path as _Path
        from .backup import list_snapshots
        with console.status("[dim]Reading backup directory...[/dim]", spinner="dots"):
            snaps = list_snapshots()
        if not snaps:
            console.print("[yellow]No snapshots found.[/yellow]")
        else:
            console.print(f"[bold]{len(snaps)} snapshot(s):[/bold]\n")
            for s in snaps:
                # ── Build identity ──────────────────────────────────────────
                comfy_root = s.get("comfyui", {}).get("root", "")
                build_name = _Path(comfy_root).name if comfy_root else ""

                # ── Metadata from correct sub-dicts ─────────────────────────
                env  = s.get("environment", {})
                hw   = s.get("hardware", {})
                rest = s.get("restorable", {})
                py_ver   = env.get("python_version", "")
                cuda_ver = hw.get("cuda_driver_version", "")
                n_local  = len(rest.get("local_wheels", []))
                n_edit   = len(rest.get("editable", []))

                note  = f"  — {s['note']}" if s.get("note") else ""
                flags = ""
                if n_local:
                    flags += f"  {n_local} local wheel(s)"
                if n_edit:
                    flags += f"  {n_edit} editable"
                cuda_str = f"  CUDA {cuda_ver}" if cuda_ver else ""

                # ── Line 1: build name + timestamp + note ────────────────────
                build_str = f"[cyan]{build_name}[/cyan]  " if build_name else ""
                console.print(f"  {build_str}{s.get('timestamp','?')}  {s.get('package_count','?')} pkgs{note}")

                # ── Line 2: python / cuda / wheels ───────────────────────────
                details = "  ".join(filter(None, [py_ver, cuda_str.strip(), flags.strip()]))
                if details:
                    console.print(f"    [dim]{details}[/dim]")

                # ── Line 3: dirty custom nodes warning ───────────────────────
                dirty_nodes = [
                    n["name"] for n in s.get("custom_nodes", [])
                    if n.get("has_local_changes")
                ]
                if dirty_nodes:
                    names = ", ".join(dirty_nodes[:3])
                    extra = f" +{len(dirty_nodes) - 3} more" if len(dirty_nodes) > 3 else ""
                    console.print(
                        f"    [yellow]⚠ local edits in:[/yellow] [dim]{names}{extra}[/dim]"
                    )

                # ── Line 4: config files captured ────────────────────────────
                cfg = s.get("config_files", {})
                cfg_parts = []
                if "extra_model_paths_yaml" in cfg:
                    cfg_parts.append("extra_model_paths.yaml")
                if "comfyui_settings" in cfg or "comfyui_settings_raw" in cfg:
                    cfg_parts.append("comfyui.settings")
                if cfg_parts:
                    console.print(f"    [dim]config captured: {', '.join(cfg_parts)}[/dim]")

                # ── Line 5: full path ────────────────────────────────────────
                if comfy_root:
                    console.print(f"    [dim]install : {comfy_root}[/dim]")
                console.print(f"    [dim]file    : {s['file']}[/dim]")
                console.print("")
        sys.exit(0)

    # --backup
    if do_backup:
        from .backup import create_snapshot
        from .env_checks import detect_active_comfyui, find_all_comfyui_installs
        from .pip_checks import venv_python_for_root
        snap_root = _comfyui_root or detect_active_comfyui() or (find_all_comfyui_installs() or [None])[0]
        _py = venv_python_for_root(snap_root) if snap_root else None
        venv_python = str(_py) if _py else None
        _snap_msg = (
            "[dim]Collecting snapshot with SHA-256 model checksums — this may take several minutes...[/dim]"
            if do_checksums else
            "[dim]Collecting snapshot — this may take a moment...[/dim]"
        )
        try:
            with console.status(_snap_msg, spinner="dots"):
                path = create_snapshot(
                    python_exe=venv_python,
                    comfyui_root=snap_root,
                    note="manual backup",
                    checksums=do_checksums,
                )
        except (PermissionError, OSError) as _snap_err:
            console.print(f"[red]Cannot create snapshot:[/red] {_snap_err}")
            sys.exit(1)
        console.print(f"[green]Snapshot saved:[/green] {path}")
        from .backup import purge_old_snapshots
        deleted = purge_old_snapshots(keep=cfg.max_backups)
        if deleted:
            console.print(f"[dim]Retention policy: removed {deleted} old snapshot(s) (max_backups={cfg.max_backups})[/dim]")
        sys.exit(0)

    # --backup-workflows
    if do_backup_workflows:
        from .backup import create_workflow_snapshot
        _wf_root = _comfyui_root
        if _wf_root is None:
            from .env_checks import detect_active_comfyui, find_all_comfyui_installs
            _wf_root = detect_active_comfyui() or (find_all_comfyui_installs() or [None])[0]
        if _wf_root is None:
            console.print("[red]Cannot locate ComfyUI installation. Use --comfyui-path.[/red]")
            sys.exit(1)
        try:
            with console.status("[dim]Archiving workflow files...[/dim]", spinner="dots"):
                wf_path = create_workflow_snapshot(_wf_root, note="manual backup")
        except (PermissionError, OSError) as _wf_err:
            console.print(f"[red]Cannot create workflow snapshot:[/red] {_wf_err}")
            sys.exit(1)
        if wf_path is None:
            console.print("[yellow]No workflow files found (checked user/default/workflows/ and workflows/).[/yellow]")
            sys.exit(1)
        console.print(f"[green]Workflow snapshot saved:[/green] {wf_path}")
        sys.exit(0)

    # --list-workflow-backups
    if do_list_workflow_backups:
        from .backup import list_workflow_snapshots
        snaps = list_workflow_snapshots()
        if not snaps:
            console.print("[yellow]No workflow snapshots found.[/yellow]")
        else:
            console.print(f"[bold]{len(snaps)} workflow snapshot(s):[/bold]\n")
            for s in snaps:
                ts = s.get("timestamp", "?")
                root_str = s.get("comfyui_root", "")
                count = s.get("workflow_count", "?")
                note = f"  — {s['note']}" if s.get("note") else ""
                console.print(f"  {ts}  {count} workflow(s){note}")
                if root_str:
                    console.print(f"    [dim]install : {root_str}[/dim]")
                console.print(f"    [dim]file    : {s['file']}[/dim]")
                console.print("")
        sys.exit(0)

    # --missing-models
    if do_missing_models:
        from .backup import list_missing_models, list_snapshots_for, list_snapshots
        _mm_root = _comfyui_root
        if _mm_root is None:
            from .env_checks import detect_active_comfyui
            _mm_root = detect_active_comfyui()
        snaps = (list_snapshots_for(_mm_root) if _mm_root else list_snapshots())
        if not snaps:
            console.print("[yellow]No snapshots found.[/yellow]")
            sys.exit(1)
        snap_data = snaps[0]
        snap_file = snap_data["file"]
        if _mm_root is None:
            # Fall back to the root stored inside the snapshot
            _mm_root_str = snap_data.get("comfyui", {}).get("root", "")
            if not _mm_root_str:
                console.print("[red]Cannot determine ComfyUI root. Use --comfyui-path.[/red]")
                sys.exit(1)
            _mm_root = Path(_mm_root_str)
        with console.status("[dim]Diffing model inventory against latest snapshot...[/dim]", spinner="dots"):
            missing = list_missing_models(snap_file, _mm_root)
        if not missing:
            console.print("[green]All models present — nothing missing.[/green]")
        else:
            console.print(f"[yellow]{len(missing)} model(s) missing from disk:[/yellow]\n")
            for m in missing:
                size_str = f"  ({m['size_gb']:.1f} GB)" if m["size_gb"] else ""
                console.print(f"  [dim]{m['category']}/[/dim]{m['name']}{size_str}")
        sys.exit(0)

    # --restore-models
    if restore_models_path is not None:
        from .backup import restore_models, list_snapshots_for, list_snapshots
        _rm_root = _comfyui_root
        if _rm_root is None:
            from .env_checks import detect_active_comfyui
            _rm_root = detect_active_comfyui()
        if restore_models_path == "latest":
            snaps = list_snapshots_for(_rm_root) if _rm_root else list_snapshots()
            if not snaps:
                console.print("[red]No snapshots found.[/red]")
                sys.exit(1)
            snap_file = snaps[0]["file"]
            if _rm_root is None:
                _rm_root_str = snaps[0].get("comfyui", {}).get("root", "")
                if _rm_root_str:
                    _rm_root = Path(_rm_root_str)
        else:
            snap_file = restore_models_path
        if _rm_root is None:
            console.print("[red]Cannot determine ComfyUI root. Use --comfyui-path.[/red]")
            sys.exit(1)
        ok, msgs = restore_models(snap_file, _rm_root, dry_run=dry_run)
        for m in msgs:
            console.print(m)
        sys.exit(0 if ok else 1)

    # --restore
    if restore_path is not None:
        from .backup import restore_snapshot, list_snapshots
        from .pip_checks import venv_python_for_root
        snap = None if restore_path == "latest" else restore_path
        _py = venv_python_for_root(_comfyui_root) if _comfyui_root else None
        venv_python = str(_py) if _py else None
        console.print("[dim]Loading snapshot, verifying environment...[/dim]")
        ok, msgs = restore_snapshot(
            snapshot_path=snap,
            python_exe=venv_python,
            comfyui_root=_comfyui_root,
            dry_run=dry_run,
        )
        for m in msgs:
            console.print(m)
        sys.exit(0 if ok else 1)

    # --pip-check: core package compatibility only
    if do_pip_check:
        import traceback as _tb
        from .pip_checks import run_pip_checks
        from .env_checks import EnvCheckReport, STATUS_FAIL, STATUS_WARN
        try:
            with console.status("[dim]Probing packages in ComfyUI venv...[/dim]", spinner="dots"):
                rows = run_pip_checks(comfyui_root=_comfyui_root)
        except Exception as _exc:
            console.print(f"\n[red][FAIL] Package check crashed unexpectedly:[/red] {_exc}")
            console.print("[dim]" + _tb.format_exc() + "[/dim]")
            sys.exit(1)
        _report = EnvCheckReport(rows=rows)
        render_env_report(_report)

        if fix:
            fixable = [r for r in rows if r.fix_cmd and r.status in (STATUS_FAIL, STATUS_WARN)]
            if not fixable:
                console.print("\n[green]Nothing to fix.[/green]")
                sys.exit(0)

            console.print("\n[bold]Fixable issues:[/bold]")
            for i, r in enumerate(fixable, 1):
                color = "red" if r.status == STATUS_FAIL else "yellow"
                console.print(f"  [{color}]{i}[/{color}]  {r.check}: {r.detail}")
                console.print(f"     [dim]$ {r.fix_cmd}[/dim]")

            console.print("\nEnter numbers to fix (e.g. 1,3), [bold]a[/bold] for all, or [bold]q[/bold] to cancel:")
            choice = input("> ").strip().lower()

            if choice == "q" or not choice:
                sys.exit(0)

            if choice == "a":
                selected = fixable
            else:
                indices = []
                for part in choice.replace(" ", "").split(","):
                    try:
                        idx = int(part) - 1
                        if 0 <= idx < len(fixable):
                            indices.append(idx)
                    except ValueError:
                        pass
                selected = [fixable[i] for i in indices]

            if not selected:
                console.print("[yellow]No valid selection.[/yellow]")
                sys.exit(1)

            # Snapshot before making changes
            from .backup import create_snapshot
            venv_python = None
            for r in rows:
                if r.check == "ComfyUI venv Python" and r.status == "[OK]":
                    import re as _re
                    m = _re.match(r"^(\S+)", r.detail)
                    if m:
                        venv_python = m.group(1)
                    break
            snap = create_snapshot(python_exe=venv_python, comfyui_root=_comfyui_root, note="before pip-check fix")
            console.print(f"[dim]Snapshot saved: {snap}[/dim]\n")

            for r in selected:
                console.print(f"\n[dim]$ {r.fix_cmd}[/dim]")
                _run_fix_with_progress(r.check, r.fix_cmd)

            console.print("\n[bold]Re-running check...[/bold]\n")
            rows2 = run_pip_checks(comfyui_root=_comfyui_root)
            render_env_report(EnvCheckReport(rows=rows2))

        sys.exit(0 if _report.failures == 0 else 1)

    # --env-check path
    if do_env_check:
        if _comfyui_root:
            import os as _os
            _os.environ["COMFYUI_PATH"] = str(_comfyui_root)
        with console.status("[dim]Scanning environment...[/dim]", spinner="dots"):
            report = run_env_checks(fix=fix)
        render_env_report(report)
        sys.exit(0 if report.failures == 0 else 1)

    effective_url = url or cfg.url
    effective_interval = interval or cfg.interval
    vram_warn = alert_vram or cfg.thresholds["vram_warn_pct"]
    vram_crit = alert_vram_critical or cfg.thresholds["vram_critical_pct"]
    queue_warn = alert_queue or cfg.thresholds["queue_warn"]
    stale_minutes = cfg.thresholds["stale_job_minutes"]
    history_jobs = cfg.thresholds["history_jobs"]
    _disk_warn_bytes = int(cfg.thresholds.get("disk_warn_gb", 20.0) * (1024 ** 3))
    _disk_crit_bytes = int(cfg.thresholds.get("disk_critical_gb", 5.0) * (1024 ** 3))

    wh_cfg = cfg.webhooks
    _discord_url = wh_cfg.get("discord_url") or None
    _ntfy_url    = wh_cfg.get("ntfy_url") or None
    _wh_on_warn  = bool(wh_cfg.get("on_warn", False))
    try:
        _wh_interval = float(wh_cfg.get("min_interval_seconds", 300))
    except (TypeError, ValueError):
        _wh_interval = 300.0

    def _maybe_webhook(r: FullHealthReport) -> None:
        if not _discord_url and not _ntfy_url:
            return
        from .webhooks import fire_webhooks
        msgs = fire_webhooks(r, _discord_url, _ntfy_url, _wh_on_warn, _wh_interval)
        for m in msgs:
            console.print(f"[dim]{m}[/dim]")

    _running_since: dict[str, float] = {}

    async def _run_once(rs: dict[str, float] | None = None) -> FullHealthReport:
        return await _collect_report(
            url=effective_url,
            config=cfg,
            vram_warn=vram_warn,
            vram_crit=vram_crit,
            queue_warn=queue_warn,
            stale_minutes=stale_minutes,
            history_jobs=history_jobs,
            running_since=rs,
            comfyui_path=str(_comfyui_root) if _comfyui_root else None,
        )

    if output_json:
        with console.status("[dim]Fetching ComfyUI data...[/dim]", spinner="dots"):
            report = asyncio.run(_run_once())
        json_str = report.model_dump_json(indent=2)
        # Write as UTF-8 bytes to avoid cp1252/Windows console encoding errors
        # (ComfyUI nodes/model names can contain emoji and other non-ASCII)
        try:
            sys.stdout.buffer.write(json_str.encode("utf-8"))
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except AttributeError:
            # stdout has no .buffer (e.g. piped through an encoding wrapper)
            click.echo(json_str)
        # Webhooks in a daemon thread so a slow/unreachable server doesn't
        # block stdout — the JSON was already written above.  Join with a
        # generous timeout (5 s per URL × up to 2 URLs + 1 s margin) so the
        # attempt completes in the normal case; daemon=True lets the process
        # exit promptly if the server still hasn't responded after 11 s.
        _wh_t = threading.Thread(target=_maybe_webhook, args=(report,), daemon=True)
        _wh_t.start()
        _wh_t.join(timeout=11.0)
        sys.exit(0 if report.overall_status != HealthStatus.CRITICAL else 1)

    if watch:
        from rich.live import Live
        from .dashboard import build_report_renderable_live
        from .env_checks import detect_active_comfyui, find_all_comfyui_installs

        _last_active: Path | None = detect_active_comfyui()
        _last_installs: int = len(find_all_comfyui_installs())

        # ── Quit-key listener ────────────────────────────────────────────────
        # Runs in a daemon thread so it never blocks the refresh loop.
        # Accepts: Esc (primary), Q / q, Ctrl+Q, Ctrl+C
        # On Windows: msvcrt.getch() gives true single-keypress detection.
        # On Unix:    tty.setraw() lets us read one byte without Enter.
        _quit_event = threading.Event()

        def _quit_listener() -> None:
            try:
                import msvcrt  # Windows
                # Use blocking getch() — no kbhit() poll needed.
                # kbhit() can report False while Rich owns the output buffer,
                # but getch() always blocks on the true console input stream.
                while not _quit_event.is_set():
                    key = msvcrt.getch()  # blocks until a key is pressed
                    if key in (b"q", b"Q", b"\x11", b"\x1b", b"\x03"):
                        _quit_event.set()
                        return
                    # Extended keys (arrows etc.) arrive as two bytes; consume both
                    if key in (b"\x00", b"\xe0"):
                        msvcrt.getch()
            except ImportError:
                # Unix — switch stdin to raw mode for single-char reads
                try:
                    import tty, termios, select as _sel
                    fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(fd)
                    try:
                        tty.setraw(fd)
                        while not _quit_event.is_set():
                            if _sel.select([sys.stdin], [], [], 0.1)[0]:
                                key = sys.stdin.read(1)
                                if key in ("q", "Q", "\x11", "\x1b", "\x03"):
                                    _quit_event.set()
                                    return
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass  # non-interactive stdin — rely on Ctrl+C / KeyboardInterrupt
            except Exception:
                pass

        _listener_thread = threading.Thread(target=_quit_listener, daemon=True)
        _listener_thread.start()

        with console.status("[dim]Connecting to ComfyUI...[/dim]", spinner="dots"):
            report = asyncio.run(_run_once(_running_since))

        try:
            # Run fetches in a background thread so the display loop (and
            # countdown) never freezes while waiting for the network.
            # _pending holds the latest completed result; _fetching guards
            # against launching a second fetch before the first finishes.
            _pending: list[FullHealthReport] = []
            _fetching = threading.Event()

            def _bg_fetch() -> None:
                try:
                    _pending.append(asyncio.run(_run_once(_running_since)))
                except BaseException:
                    # asyncio.run() itself raised (e.g. CancelledError, RuntimeError
                    # from a broken event loop).  Push a synthetic offline report so
                    # the countdown clock resets instead of staying at 0 and spawning
                    # a new thread every 250 ms in a busy-loop.
                    _fallback = FullHealthReport(
                        comfyui_url=effective_url,
                        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    )
                    from .checks import check_connectivity as _cc
                    _fallback.checks.append(_cc(None, url=effective_url))
                    from .models import HealthStatus as _HS
                    _fallback.overall_status = _HS.CRITICAL
                    _fallback.alerts.append("Fetch loop error — monitoring interrupted")
                    _pending.append(_fallback)
                finally:
                    _fetching.clear()

            _next_fetch_at = time.time() + effective_interval

            with Live(
                build_report_renderable_live(report, effective_interval, effective_interval, _disk_warn_bytes, _disk_crit_bytes),
                console=console,
                refresh_per_second=4,
                auto_refresh=False,
            ) as live:
                while not _quit_event.is_set():
                    now = time.time()

                    # When the countdown reaches zero: fire a fetch and, once the
                    # result arrives, apply it and restart the clock.  Results are
                    # never applied mid-countdown — the display only updates at the
                    # boundary between cycles.
                    next_in = _next_fetch_at - now
                    if next_in <= 0:
                        if not _fetching.is_set():
                            _fetching.set()
                            threading.Thread(target=_bg_fetch, daemon=True).start()

                        if _pending:
                            report = _pending.pop(0)
                            # Fire webhooks in a daemon thread — httpx.post() is
                            # synchronous and can block for up to `timeout` seconds
                            # per URL.  Calling it here would freeze the Live display.
                            threading.Thread(
                                target=_maybe_webhook, args=(report,), daemon=True
                            ).start()

                            _current_active = detect_active_comfyui()
                            if _current_active != _last_active:
                                _last_active = _current_active

                            _current_count = len(find_all_comfyui_installs())
                            if _current_count != _last_installs:
                                _last_installs = _current_count

                            _next_fetch_at = time.time() + effective_interval

                    live.update(
                        build_report_renderable_live(report, effective_interval, max(0.0, next_in), _disk_warn_bytes, _disk_crit_bytes),
                        refresh=True,
                    )
                    time.sleep(0.25)

        except KeyboardInterrupt:
            _quit_event.set()
    else:
        with console.status("[dim]Connecting to ComfyUI...[/dim]", spinner="dots"):
            report = asyncio.run(_run_once())
        render_full_report(report, _disk_warn_bytes, _disk_crit_bytes)
        # Same daemon-thread + join pattern as the JSON path and the watch loop:
        # an unreachable webhook URL must not block the process from exiting.
        _wh_t = threading.Thread(target=_maybe_webhook, args=(report,), daemon=True)
        _wh_t.start()
        _wh_t.join(timeout=11.0)
        if html_output:
            from .html_export import export_html
            try:
                out = export_html(report, html_output, comfyui_path=_comfyui_root)
                console.print(f"[green]HTML report saved:[/green] {out}")
            except (PermissionError, OSError) as _e:
                console.print(f"[red]Cannot write HTML report:[/red] {_e}")
        sys.exit(0 if report.overall_status != HealthStatus.CRITICAL else 1)


if __name__ == "__main__":
    main()
