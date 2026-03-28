"""
Webhook notifications for ComfyUI Health Monitor.

Supports Discord (via webhook URL) and ntfy.sh (via topic URL).
Auto-detected by URL pattern. Fires on CRITICAL or WARN status transitions.

Config (comfyui-health.toml):
    [webhooks]
    discord_url = "https://discord.com/api/webhooks/..."
    ntfy_url    = "https://ntfy.sh/your-topic"
    on_warn     = false   # default: only fire on CRITICAL
    min_interval_seconds = 300  # don't re-fire within 5 min
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import FullHealthReport, HealthStatus

# Colour codes for Discord embeds
_COLOUR = {
    "OK": 0x2ECC71,       # green
    "WARN": 0xF39C12,     # amber
    "CRITICAL": 0xE74C3C, # red
    "UNKNOWN": 0x95A5A6,  # grey
}

# Rate-limit state (per webhook URL)
_last_fired: dict[str, float] = {}


def _should_fire(url: str, min_interval: float) -> bool:
    now = time.time()
    last = _last_fired.get(url, 0.0)
    if now - last >= min_interval:
        _last_fired[url] = now
        return True
    return False


def _is_discord(url: str) -> bool:
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


def _is_ntfy(url: str) -> bool:
    return "ntfy.sh" in url


def _build_discord_payload(report: "FullHealthReport") -> dict[str, Any]:
    from .models import HealthStatus
    status = report.overall_status.value if hasattr(report.overall_status, "value") else str(report.overall_status)
    colour = _COLOUR.get(status, _COLOUR["UNKNOWN"])

    checks_ok   = sum(1 for c in report.checks if c.status == HealthStatus.OK)
    checks_warn = sum(1 for c in report.checks if c.status == HealthStatus.WARN)
    checks_crit = sum(1 for c in report.checks if c.status == HealthStatus.CRITICAL)

    alerts_text = "\n".join(f"• {a}" for a in report.alerts[:8]) or "None"
    if len(report.alerts) > 8:
        alerts_text += f"\n… and {len(report.alerts) - 8} more"

    fields = [
        {"name": "Instance", "value": report.comfyui_url, "inline": True},
        {"name": "Status", "value": status, "inline": True},
        {"name": "Checks", "value": f"{checks_ok} OK  {checks_warn} WARN  {checks_crit} CRIT", "inline": True},
    ]

    if report.system_stats:
        s = report.system_stats
        vram_str = ""
        for dev in [d for d in s.devices if d.type != "cpu"]:
            # DeviceInfo stores raw bytes: vram_total, vram_free (no *_mb fields)
            used_gb  = (dev.vram_total - dev.vram_free) / 1024 ** 3
            total_gb = dev.vram_total / 1024 ** 3
            pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
            vram_str += f"{dev.name}: {used_gb:.1f}/{total_gb:.1f} GB ({pct:.0f}%)\n"
        if vram_str:
            fields.append({"name": "VRAM", "value": vram_str.strip(), "inline": False})

    if report.queue_stats:
        q = report.queue_stats
        fields.append({"name": "Queue", "value": f"{q.running_count} running  {q.pending_count} pending", "inline": True})

    fields.append({"name": "Alerts", "value": alerts_text, "inline": False})

    return {
        "embeds": [{
            "title": f"ComfyUI Health Monitor — {status}",
            "description": report.timestamp,
            "color": colour,
            "fields": fields,
            "footer": {"text": "comfyui-health"},
        }]
    }


def _build_ntfy_payload(report: "FullHealthReport") -> tuple[str, dict[str, str]]:
    """Return (body, headers) for ntfy."""
    from .models import HealthStatus
    status = report.overall_status.value if hasattr(report.overall_status, "value") else str(report.overall_status)

    priority = "urgent" if report.overall_status == HealthStatus.CRITICAL else "high"
    tags = "rotating_light,computer" if report.overall_status == HealthStatus.CRITICAL else "warning,computer"

    body_lines = [f"ComfyUI @ {report.comfyui_url}", ""]
    for alert in report.alerts[:6]:
        body_lines.append(f"• {alert}")
    if not report.alerts:
        body_lines.append("No active alerts.")

    headers = {
        "Title": f"ComfyUI {status}",
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain",
    }
    return "\n".join(body_lines), headers


def fire_webhooks(
    report: "FullHealthReport",
    discord_url: str | None,
    ntfy_url: str | None,
    on_warn: bool = False,
    min_interval: float = 300.0,
) -> list[str]:
    """
    Send webhooks if the report status warrants it.
    Returns a list of status messages (for logging).
    """
    from .models import HealthStatus

    status = report.overall_status
    should_alert = (status == HealthStatus.CRITICAL) or (on_warn and status == HealthStatus.WARN)
    if not should_alert:
        return []

    messages: list[str] = []

    try:
        import httpx
    except ImportError:
        return ["[webhooks] httpx not installed — cannot send webhooks"]

    # 5 s timeout: webhooks are fire-and-forget notifications.  The caller
    # (watch loop) runs this in a background thread, but a shorter timeout
    # keeps the thread alive for less time and frees the rate-limit slot
    # sooner if the service is reachable but slow.
    _WH_TIMEOUT = 5

    if discord_url and _should_fire(discord_url, min_interval):
        try:
            payload = _build_discord_payload(report)
            r = httpx.post(discord_url, json=payload, timeout=_WH_TIMEOUT)
            if r.status_code in (200, 204):
                messages.append(f"[webhooks] Discord fired ({r.status_code})")
            else:
                messages.append(f"[webhooks] Discord failed: HTTP {r.status_code} — {r.content[:120].decode('utf-8', errors='replace')}")
        except Exception as e:
            messages.append(f"[webhooks] Discord error: {e}")

    if ntfy_url and _should_fire(ntfy_url, min_interval):
        try:
            body, headers = _build_ntfy_payload(report)
            r = httpx.post(ntfy_url, content=body.encode(), headers=headers, timeout=_WH_TIMEOUT)
            if r.status_code in (200, 204):
                messages.append(f"[webhooks] ntfy fired ({r.status_code})")
            else:
                messages.append(f"[webhooks] ntfy failed: HTTP {r.status_code} — {r.content[:120].decode('utf-8', errors='replace')}")
        except Exception as e:
            messages.append(f"[webhooks] ntfy error: {e}")

    return messages
