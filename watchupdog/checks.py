"""Individual health check functions -each returns a HealthCheckResult."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    GenerationStats,
    HealthCheckResult,
    HealthStatus,
    JobRecord,
    JobStatus,
    NunchakuInfo,
    QueueStats,
    SystemStats,
)
from .nunchaku import check_nunchaku_vram_anomaly


def check_connectivity(
    system_stats: dict[str, Any] | None,
    url: str = "",
) -> HealthCheckResult:
    if system_stats is None:
        url_hint = f" ({url})" if url else ""
        return HealthCheckResult(
            name="connectivity",
            status=HealthStatus.CRITICAL,
            message=f"ComfyUI unreachable{url_hint} — no response from /system_stats",
        )
    return HealthCheckResult(
        name="connectivity",
        status=HealthStatus.OK,
        message="ComfyUI is reachable",
    )


def check_queue_health(
    queue_data: dict[str, Any] | None,
    warn_threshold: int = 10,
) -> tuple[HealthCheckResult, QueueStats]:
    if queue_data is None:
        return (
            HealthCheckResult(
                name="queue",
                status=HealthStatus.UNKNOWN,
                message="Queue data unavailable",
            ),
            QueueStats(),
        )

    # Both fields may be absent, null, or wrong type — normalise to lists
    # before handing to QueueStats (Pydantic rejects None for list[Any] fields).
    queue_running = queue_data.get("queue_running")
    queue_pending = queue_data.get("queue_pending")
    if not isinstance(queue_running, list):
        queue_running = []
    if not isinstance(queue_pending, list):
        queue_pending = []
    stats = QueueStats(running=queue_running, pending=queue_pending)

    pending = stats.pending_count
    running = stats.running_count

    if pending > warn_threshold:
        status = HealthStatus.WARN
        message = f"Queue backlog: {pending} pending, {running} running (threshold: {warn_threshold})"
    else:
        status = HealthStatus.OK
        message = f"Queue OK: {pending} pending, {running} running"

    return (
        HealthCheckResult(
            name="queue",
            status=status,
            message=message,
            details={"pending": pending, "running": running},
        ),
        stats,
    )


def check_stale_jobs(
    queue_data: dict[str, Any] | None,
    stale_minutes: float = 5.0,
    running_since: dict[str, float] | None = None,
) -> HealthCheckResult:
    """Warn if a job has been 'running' longer than stale_minutes.

    running_since: mutable dict {prompt_id: first_seen_timestamp} maintained by caller.
                   When None, staleness cannot be measured (returns INFO instead).
    """
    if queue_data is None:
        return HealthCheckResult(
            name="stale_jobs",
            status=HealthStatus.UNKNOWN,
            message="Queue data unavailable",
        )

    queue_running = queue_data.get("queue_running")
    if not isinstance(queue_running, list):
        queue_running = []

    if not queue_running:
        if running_since is not None:
            running_since.clear()
        return HealthCheckResult(
            name="stale_jobs",
            status=HealthStatus.OK,
            message="No jobs currently running",
        )

    # Extract prompt IDs
    current_ids: list[str] = []
    for entry in queue_running:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            current_ids.append(str(entry[1]))
        elif isinstance(entry, dict):
            current_ids.append(entry.get("prompt_id", "unknown"))

    if running_since is None:
        return HealthCheckResult(
            name="stale_jobs",
            status=HealthStatus.OK,
            message=f"{len(queue_running)} job(s) running (staleness tracking requires watch mode)",
            details={"running_ids": current_ids},
        )

    now = time.time()
    stale_threshold = stale_minutes * 60

    # Register newly seen jobs
    for pid in current_ids:
        if pid not in running_since:
            running_since[pid] = now

    # Remove jobs no longer in queue
    for pid in list(running_since):
        if pid not in current_ids:
            del running_since[pid]

    # Find stale jobs
    stale: list[str] = [
        pid for pid, first_seen in running_since.items()
        if now - first_seen >= stale_threshold
    ]

    if stale:
        longest = max(running_since[pid] for pid in stale)
        elapsed_min = (now - longest) / 60
        return HealthCheckResult(
            name="stale_jobs",
            status=HealthStatus.WARN,
            message=f"{len(stale)} stale job(s) running > {stale_minutes:.0f}m (longest: {elapsed_min:.0f}m)",
            details={"stale_ids": stale, "all_running": current_ids},
        )

    return HealthCheckResult(
        name="stale_jobs",
        status=HealthStatus.OK,
        message=f"{len(queue_running)} job(s) running (all within {stale_minutes:.0f}m threshold)",
        details={"running_ids": current_ids},
    )


def check_vram_health(
    system_stats: dict[str, Any] | None,
    warn_pct: float = 90.0,
    critical_pct: float = 97.0,
    nunchaku: NunchakuInfo | None = None,
    nunchaku_anomaly_gb: float = 14.0,
    nunchaku_min_card_gb: float = 12.0,
) -> tuple[HealthCheckResult, SystemStats]:
    if system_stats is None:
        parsed = _parse_system_stats(system_stats)
        return (
            HealthCheckResult(
                name="vram",
                status=HealthStatus.UNKNOWN,
                message="No GPU device info available — ComfyUI unreachable",
            ),
            parsed,
        )

    parsed = _parse_system_stats(system_stats)

    # Check if all devices are CPU type (no discrete GPU)
    non_cpu = [d for d in parsed.devices if d.type != "cpu"]
    if not parsed.devices or not non_cpu:
        return (
            HealthCheckResult(
                name="vram",
                status=HealthStatus.OK,
                message="CPU-only mode — no discrete GPU VRAM to monitor",
            ),
            parsed,
        )

    alerts: list[str] = []
    worst_status = HealthStatus.OK
    details: dict[str, Any] = {}

    for dev in parsed.devices:
        if dev.vram_total <= 0:
            continue
        # _sanitise_vram (called in _parse_system_stats) guarantees
        # 0 <= vram_free <= vram_total, so used >= 0 and pct <= 100.
        # The explicit clamps below are a second line of defence in case
        # DeviceInfo is constructed directly without going through the parser.
        used = max(0, dev.vram_total - dev.vram_free)
        pct  = min(100.0, (used / dev.vram_total) * 100)
        details[dev.name] = {
            "used_gb": round(used / (1024**3), 2),
            "total_gb": round(dev.vram_total / (1024**3), 2),
            "pct": round(pct, 1),
        }

        if pct >= critical_pct:
            worst_status = HealthStatus.CRITICAL
            alerts.append(f"{dev.name}: VRAM {pct:.0f}% -CRITICAL")
        elif pct >= warn_pct:
            if worst_status == HealthStatus.OK:
                worst_status = HealthStatus.WARN
            alerts.append(f"{dev.name}: VRAM {pct:.0f}% -WARN")

        # Nunchaku anomaly check
        if nunchaku:
            anomaly = check_nunchaku_vram_anomaly(
                nunchaku, used, dev.vram_total,
                anomaly_gb=nunchaku_anomaly_gb,
                min_card_gb=nunchaku_min_card_gb,
            )
            if anomaly:
                if worst_status == HealthStatus.OK:
                    worst_status = HealthStatus.WARN
                alerts.append(anomaly)

    if not alerts:
        # Use the first non-CPU device, not devices[0] which may be a CPU entry.
        first_dev = non_cpu[0] if non_cpu else parsed.devices[0]
        if first_dev.vram_total == 0:
            # vram_total was absent or a wrong type in the API response — do not
            # claim "VRAM OK: 0.0 / 0.0 GB" which looks like a healthy reading.
            return (
                HealthCheckResult(
                    name="vram",
                    status=HealthStatus.UNKNOWN,
                    message=f"VRAM data unavailable for {first_dev.name} — API returned 0 bytes",
                ),
                parsed,
            )

        if len(non_cpu) == 1:
            used = max(0, first_dev.vram_total - first_dev.vram_free)
            pct  = min(100.0, used / first_dev.vram_total * 100)
            message = (
                f"VRAM OK: {used / (1024**3):.1f} GB / "
                f"{first_dev.vram_total / (1024**3):.1f} GB ({pct:.0f}%)"
            )
        else:
            # Multi-GPU: summarise every device so all are visible when healthy.
            parts: list[str] = []
            for dev in non_cpu:
                if dev.vram_total == 0:
                    parts.append(f"{dev.name}: unavailable")
                else:
                    used = max(0, dev.vram_total - dev.vram_free)
                    pct  = min(100.0, used / dev.vram_total * 100)
                    parts.append(
                        f"{dev.name}: "
                        f"{used / (1024**3):.1f}/{dev.vram_total / (1024**3):.1f} GB ({pct:.0f}%)"
                    )
            message = "VRAM OK — " + "  |  ".join(parts)
    else:
        message = "; ".join(alerts)

    return (
        HealthCheckResult(
            name="vram",
            status=worst_status,
            message=message,
            details=details,
        ),
        parsed,
    )


def check_ram_health(
    system_stats: dict[str, Any] | None,
    warn_pct: float = 85.0,
) -> HealthCheckResult:
    from .metrics import get_ram_info

    parsed = _parse_system_stats(system_stats)

    if parsed.ram_total == 0:
        # ComfyUI /system_stats doesn't expose RAM — read it locally via psutil
        local = get_ram_info()
        if local:
            parsed.ram_total = local["total"]
            parsed.ram_used = local["used"]
            parsed.ram_free = local["free"]

    if parsed.ram_total == 0:
        return HealthCheckResult(
            name="ram",
            status=HealthStatus.UNKNOWN,
            message="RAM info unavailable (psutil not installed)",
        )

    used = parsed.ram_used
    pct = (used / parsed.ram_total) * 100

    if pct >= warn_pct:
        status = HealthStatus.WARN
        message = (
            f"RAM usage {pct:.0f}% -"
            f"{used / (1024**3):.1f} GB / {parsed.ram_total / (1024**3):.1f} GB"
        )
    else:
        status = HealthStatus.OK
        message = (
            f"RAM OK: {used / (1024**3):.1f} GB / "
            f"{parsed.ram_total / (1024**3):.1f} GB ({pct:.0f}%)"
        )

    return HealthCheckResult(
        name="ram",
        status=status,
        message=message,
        details={"used_gb": round(used / (1024**3), 2), "pct": round(pct, 1)},
    )


def check_nunchaku_nodes(
    nunchaku: NunchakuInfo,
) -> HealthCheckResult:
    if not nunchaku.nodes_found:
        return HealthCheckResult(
            name="nunchaku_nodes",
            status=HealthStatus.WARN,
            message="No Nunchaku nodes found in /object_info -ComfyUI-nunchaku may not be installed",
        )

    if nunchaku.wheel_installer_present and not nunchaku.dit_loader_present:
        return HealthCheckResult(
            name="nunchaku_nodes",
            status=HealthStatus.WARN,
            message="NunchakuWheelInstaller found but NunchakuFluxDiTLoader missing -wheel not yet installed?",
            details={"nodes_found": nunchaku.nodes_found},
        )

    found_str = ", ".join(nunchaku.nodes_found)
    extra: list[str] = []
    if nunchaku.precision_mode:
        extra.append(f"precision={nunchaku.precision_mode}")
    if nunchaku.fb_cache_enabled:
        extra.append("FB_Cache=ON")
    if nunchaku.version:
        extra.append(f"v{nunchaku.version}")

    suffix = f" [{', '.join(extra)}]" if extra else ""
    return HealthCheckResult(
        name="nunchaku_nodes",
        status=HealthStatus.OK,
        message=f"Nunchaku nodes: {found_str}{suffix}",
        details={
            "nodes": nunchaku.nodes_found,
            "precision": nunchaku.precision_mode,
            "fb_cache": nunchaku.fb_cache_enabled,
            "version": nunchaku.version,
        },
    )


def check_error_rate(
    history_data: dict[str, Any] | None,
    history_jobs: int = 50,
) -> tuple[HealthCheckResult, GenerationStats]:
    if history_data is None:
        return (
            HealthCheckResult(
                name="error_rate",
                status=HealthStatus.UNKNOWN,
                message="History data unavailable",
            ),
            GenerationStats(),
        )

    jobs = _parse_history(history_data, history_jobs)
    if not jobs:
        return (
            HealthCheckResult(
                name="error_rate",
                status=HealthStatus.UNKNOWN,
                message="No job history — fresh install or history cleared",
            ),
            GenerationStats(),
        )

    completed  = sum(1 for j in jobs if j.status == JobStatus.SUCCESS)
    cancelled  = sum(1 for j in jobs if j.status == JobStatus.INTERRUPTED)
    failed     = sum(1 for j in jobs if j.status == JobStatus.ERROR)
    total      = len(jobs)
    error_rate = (failed / total) * 100 if total else 0.0

    # Most recent completion timestamp across all finished jobs
    finish_times = [
        j.completed_at_ms for j in jobs if j.completed_at_ms is not None
    ]
    last_completed = max(finish_times) if finish_times else None

    # Average execution time — only include successful jobs with timing data
    timed_jobs = [
        j.exec_time_ms for j in jobs
        if j.status == JobStatus.SUCCESS and j.exec_time_ms is not None
    ]
    avg_exec = (sum(timed_jobs) / len(timed_jobs)) if timed_jobs else None

    stats = GenerationStats(
        total_jobs=total,
        completed_jobs=completed,
        cancelled_jobs=cancelled,
        failed_jobs=failed,
        error_rate_pct=round(error_rate, 1),
        last_completed_ms=last_completed,
        avg_exec_time_ms=avg_exec,
    )

    if error_rate > 20:
        status = HealthStatus.WARN
        message = f"High error rate: {error_rate:.0f}% — {failed} of {total} jobs failed"
    elif error_rate > 0:
        status = HealthStatus.OK
        message = f"{completed} completed, {failed} failed, {cancelled} cancelled"
    else:
        status = HealthStatus.OK
        message = f"{completed} completed" + (f", {cancelled} cancelled" if cancelled else "")

    return (
        HealthCheckResult(
            name="error_rate",
            status=status,
            message=message,
            details={"error_rate_pct": error_rate, "total": total, "failed": failed},
        ),
        stats,
    )


def check_symlinks(
    comfyui_path: "str | Path | None",
    warn_gb: float = 20.0,
    critical_gb: float = 5.0,
) -> "tuple[HealthCheckResult, list]":
    """
    Scan <comfyui_path>/models/ for symlinks and directory junctions.

    Returns (HealthCheckResult, list[SymlinkEntry]).

    Surfaces:
    - WARN / CRITICAL if a cross-drive model target is low on disk space
    - WARN if any symlink target is broken (missing / unresolvable)
    - OK with a summary if everything is healthy
    - UNKNOWN if no comfyui_path was provided (single-shot without --comfyui-path)
    """
    from pathlib import Path as _Path

    if not comfyui_path:
        return (
            HealthCheckResult(
                name="symlinks",
                status=HealthStatus.UNKNOWN,
                message="Symlink check skipped — no ComfyUI path provided",
            ),
            [],
        )

    from .symlinks import scan_model_symlinks
    entries = scan_model_symlinks(comfyui_path)

    if not entries:
        return (
            HealthCheckResult(
                name="symlinks",
                status=HealthStatus.OK,
                message="No symlinked model folders detected",
            ),
            [],
        )

    # Serialisable details for JSON output and HTML export
    details: dict = {
        "total": len(entries),
        "symlinks": [
            {
                "link":          e.link_rel,
                "target":        str(e.target),
                "broken":        e.is_broken,
                "cross_drive":   e.cross_drive,
                "disk_free_gb":  round(e.disk_free_gb, 1) if e.disk_free_gb is not None else None,
                "disk_total_gb": round(e.disk_total_gb, 1) if e.disk_total_gb is not None else None,
            }
            for e in entries
        ],
    }

    # ── Broken symlinks ──────────────────────────────────────────────────────
    broken = [e for e in entries if e.is_broken]
    if broken:
        names = ", ".join(e.link_rel for e in broken[:3])
        extra = f" (+{len(broken) - 3} more)" if len(broken) > 3 else ""
        return (
            HealthCheckResult(
                name="symlinks",
                status=HealthStatus.WARN,
                message=f"{len(broken)} broken symlink(s): {names}{extra}",
                details=details,
            ),
            entries,
        )

    # ── Cross-drive disk space ───────────────────────────────────────────────
    cross = [e for e in entries if e.cross_drive and e.disk_free_gb is not None]
    if cross:
        min_free = min(e.disk_free_gb for e in cross)   # type: ignore[arg-type]
        if min_free <= critical_gb:
            return (
                HealthCheckResult(
                    name="symlinks",
                    status=HealthStatus.CRITICAL,
                    message=(
                        f"{len(entries)} symlink(s) — model drive critically low: "
                        f"{min_free:.1f} GB free"
                    ),
                    details=details,
                ),
                entries,
            )
        if min_free <= warn_gb:
            return (
                HealthCheckResult(
                    name="symlinks",
                    status=HealthStatus.WARN,
                    message=(
                        f"{len(entries)} symlink(s) — model drive low: "
                        f"{min_free:.1f} GB free"
                    ),
                    details=details,
                ),
                entries,
            )

    # ── All healthy ──────────────────────────────────────────────────────────
    healthy = len(entries)
    n_cross = len(cross)
    msg = f"{healthy} symlinked folder(s) — all targets reachable"
    if n_cross:
        min_free = min(e.disk_free_gb for e in cross)   # type: ignore[arg-type]
        msg += f" ({min_free:.1f} GB free on model drive)"

    return (
        HealthCheckResult(
            name="symlinks",
            status=HealthStatus.OK,
            message=msg,
            details=details,
        ),
        entries,
    )


def check_model_files(
    object_info: dict[str, Any] | None,
    expected_models: dict[str, list[str]] | None = None,
) -> HealthCheckResult:
    """
    Dynamically discover all models visible to ComfyUI from /object_info.
    Works with any node set — no hardcoded model names.
    If expected_models is provided, also verify those specific files are present.
    """
    if object_info is None:
        return HealthCheckResult(
            name="model_files",
            status=HealthStatus.UNKNOWN,
            message="Object info unavailable",
        )

    from .model_scanner import scan_models_from_object_info
    discovered = scan_models_from_object_info(object_info)

    total = sum(len(v) for v in discovered.values())
    if total == 0:
        return HealthCheckResult(
            name="model_files",
            status=HealthStatus.WARN,
            message="No model files detected in /object_info — ComfyUI may have no nodes loaded",
            details={"discovered": {}},
        )

    # Build summary: "Checkpoint: 3, LoRA: 47, VAE: 2"
    parts = [f"{cat}: {len(files)}" for cat, files in sorted(discovered.items())]
    summary = ", ".join(parts[:5])
    if len(parts) > 5:
        summary += f" (+{len(parts) - 5} more types)"

    # If caller specified expected files, verify they're present
    missing: list[str] = []
    if expected_models:
        all_files_lower = {f.lower() for files in discovered.values() for f in files}
        for group_files in expected_models.values():
            # At least one file from each group must be present
            if group_files and not any(m.lower() in all_files_lower for m in group_files):
                missing.append(" or ".join(group_files))

    if missing:
        return HealthCheckResult(
            name="model_files",
            status=HealthStatus.WARN,
            message=f"{summary} | Missing: {', '.join(missing[:2])}",
            details={"discovered": {k: len(v) for k, v in discovered.items()}, "missing": missing},
        )

    return HealthCheckResult(
        name="model_files",
        status=HealthStatus.OK,
        message=f"{total} models: {summary}",
        details={"discovered": {k: len(v) for k, v in discovered.items()}},
    )


def check_disk_space(
    comfyui_path: str | None = None,
    warn_gb: float = 20.0,
    critical_gb: float = 5.0,
    warn_pct: float = 90.0,
    critical_pct: float = 95.0,
) -> tuple[HealthCheckResult, tuple[int, int]]:
    """
    Check free disk space on the drive containing comfyui_path (or cwd).
    Returns (HealthCheckResult, (free_bytes, total_bytes)).
    Fires on either condition: absolute free space OR percentage used,
    whichever is more severe. AI model drives can have 60+ GB free but
    still be 97% full on a 2 TB disk, which is equally dangerous.
    """
    try:
        check_path = Path(comfyui_path) if comfyui_path else Path.cwd()
        usage = shutil.disk_usage(check_path)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        used_pct = (usage.used / usage.total) * 100 if usage.total else 0

        is_crit = free_gb <= critical_gb or used_pct >= critical_pct
        is_warn = free_gb <= warn_gb or used_pct >= warn_pct

        if is_crit:
            status = HealthStatus.CRITICAL
            message = (
                f"Disk critically full: {free_gb:.1f} GB free / {total_gb:.1f} GB "
                f"({used_pct:.0f}% used) — downloads and generations may fail"
            )
        elif is_warn:
            status = HealthStatus.WARN
            message = (
                f"Disk space low: {free_gb:.1f} GB free / {total_gb:.1f} GB "
                f"({used_pct:.0f}% used)"
            )
        else:
            status = HealthStatus.OK
            message = (
                f"Disk OK: {free_gb:.1f} GB free / {total_gb:.1f} GB "
                f"({used_pct:.0f}% used)"
            )

        return (
            HealthCheckResult(
                name="disk_space",
                status=status,
                message=message,
                details={
                    "free_gb": round(free_gb, 2),
                    "total_gb": round(total_gb, 2),
                    "used_pct": round(used_pct, 1),
                },
            ),
            (usage.free, usage.total),
        )

    except Exception as exc:
        return (
            HealthCheckResult(
                name="disk_space",
                status=HealthStatus.UNKNOWN,
                message=f"Disk check failed: {exc}",
            ),
            (0, 0),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_system_stats(raw: dict[str, Any] | None) -> SystemStats:
    if not raw:
        return SystemStats()

    from .models import DeviceInfo

    from .metrics import _sanitise_vram

    # `devices` must be a list of dicts.  Guard against null, wrong type,
    # and non-dict entries — all of which are valid JSON but crash the loop.
    devices_raw = raw.get("devices")
    if not isinstance(devices_raw, list):
        devices_raw = []

    devices = []
    for d in devices_raw:
        if not isinstance(d, dict):
            continue
        raw_total = d.get("vram_total", 0)
        raw_used  = d.get("vram_used",  0)
        raw_free  = d.get("vram_free",  0)
        # Sanitise before storing: NVML_VALUE_NOT_AVAILABLE, free > total, negatives
        # from JSON, etc. are all normalised to a coherent (total, used, free) triple.
        vram_total, _, vram_free = _sanitise_vram(
            raw_total if isinstance(raw_total, (int, float)) else 0,
            raw_used  if isinstance(raw_used,  (int, float)) else 0,
            raw_free  if isinstance(raw_free,  (int, float)) else 0,
        )
        devices.append(
            DeviceInfo(
                name=str(d.get("name", "Unknown")),
                type=str(d.get("type", "cpu")),
                index=int(d.get("index", 0)) if isinstance(d.get("index"), (int, float)) else 0,
                vram_total=vram_total,
                vram_free=vram_free,
            )
        )

    # CPU utilisation and RAM may be absent or wrong type — normalise before arithmetic.
    cpu_raw = raw.get("cpu_utilization", 0.0)
    cpu = float(cpu_raw) if isinstance(cpu_raw, (int, float)) else 0.0

    ram_total_raw = raw.get("ram_total", 0)
    ram_used_raw  = raw.get("ram_used",  0)
    ram_total = int(ram_total_raw) if isinstance(ram_total_raw, (int, float)) else 0
    ram_used  = int(ram_used_raw)  if isinstance(ram_used_raw,  (int, float)) else 0
    ram_free  = max(0, ram_total - ram_used)

    return SystemStats(
        cpu_utilization=cpu,
        ram_total=ram_total,
        ram_used=ram_used,
        ram_free=ram_free,
        devices=devices,
    )


def _parse_history(raw: dict[str, Any], max_items: int = 50) -> list[JobRecord]:
    """Parse /history response into a list of JobRecord, newest-first."""
    if not raw:
        return []

    # /history must be a dict keyed by prompt_id.  Some edge cases (empty
    # response, server returning a list) should not crash the caller.
    if not isinstance(raw, dict):
        return []

    records: list[JobRecord] = []
    items = list(raw.items())[:max_items]

    for prompt_id, data in items:
        if not isinstance(data, dict):
            continue

        # `status` is expected to be a dict; guard against string / null / absent
        status_data = data.get("status")
        if not isinstance(status_data, dict):
            status_data = {}

        # `messages` is expected to be a list of [type, data] pairs
        status_msgs = status_data.get("messages")
        if not isinstance(status_msgs, list):
            status_msgs = []

        job_status = JobStatus.UNKNOWN
        error_msg: str | None = None

        for msg in status_msgs:
            # Each entry must be a sequence of at least 2 elements: [type, data].
            # Guard against dicts, bare strings, or other unexpected shapes.
            if not isinstance(msg, (list, tuple)) or len(msg) < 2:
                continue
            msg_type, msg_data = msg[0], msg[1]
            if msg_type == "execution_success":
                job_status = JobStatus.SUCCESS
            elif msg_type == "execution_error":
                job_status = JobStatus.ERROR
                if isinstance(msg_data, dict):
                    error_msg = msg_data.get("exception_message", str(msg_data))
            elif msg_type == "execution_interrupted":
                job_status = JobStatus.INTERRUPTED

        # Compute execution time from execution_start → execution_success/error only.
        # Using max/min across all messages is wrong: early queue/status messages
        # can have timestamps from days before the job actually ran.
        start_ts: float | None = None
        end_ts: float | None = None
        for msg in status_msgs:
            if not isinstance(msg, (list, tuple)) or len(msg) < 2:
                continue
            msg_type, msg_data = msg[0], msg[1]
            if not isinstance(msg_data, dict):
                continue
            try:
                ts = float(msg_data["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if msg_type == "execution_start":
                start_ts = ts
            elif msg_type in ("execution_success", "execution_error", "execution_interrupted"):
                end_ts = ts

        exec_time: float | None = None
        if start_ts is not None and end_ts is not None and end_ts >= start_ts:
            # ComfyUI timestamps are already in ms (int(time.time() * 1000))
            exec_time = end_ts - start_ts

        records.append(
            JobRecord(
                prompt_id=prompt_id,
                status=job_status,
                completed_at_ms=end_ts,
                exec_time_ms=exec_time,
                error=error_msg,
            )
        )

    return records
