"""Optional FastAPI web dashboard server — serves metrics at :8190."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError:
    raise ImportError(
        "FastAPI and uvicorn are required for the dashboard server.\n"
        "Install with: pip install 'fastapi>=0.110' 'uvicorn>=0.29'"
    )

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
from .client import ComfyUIClient, probe_for_live_url
from .config import load_config
from .models import FullHealthReport, HealthStatus
from .nunchaku import detect_nunchaku

_cfg = load_config()
app = FastAPI(title="ComfyUI Health Dashboard", version="0.1.0")

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComfyUI Health</title>
<style>
  :root { --ok:#22c55e;--warn:#eab308;--crit:#ef4444;--unk:#6b7280;--bg:#0f172a;--card:#1e293b;--text:#f1f5f9; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; padding:1.5rem; }
  h1 { font-size:1.4rem; margin-bottom:1rem; }
  #status-badge { display:inline-block; padding:.25rem .75rem; border-radius:9999px; font-weight:700; font-size:.9rem; }
  .ok { background:var(--ok); color:#fff; }
  .warn { background:var(--warn); color:#000; }
  .crit { background:var(--crit); color:#fff; }
  .unk { background:var(--unk); color:#fff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:1rem; margin:1rem 0; }
  .card { background:var(--card); border-radius:.75rem; padding:1rem; }
  .card h2 { font-size:.85rem; text-transform:uppercase; letter-spacing:.05em; color:#94a3b8; margin-bottom:.75rem; }
  .row { display:flex; justify-content:space-between; padding:.2rem 0; font-size:.9rem; border-bottom:1px solid #334155; }
  .row:last-child { border:none; }
  .val-ok { color:var(--ok); }
  .val-warn { color:var(--warn); }
  .val-crit { color:var(--crit); }
  #alerts { margin:1rem 0; }
  .alert { background:#450a0a; border-left:4px solid var(--crit); padding:.5rem .75rem; margin:.4rem 0; border-radius:.25rem; font-size:.875rem; }
  #ts { font-size:.75rem; color:#64748b; margin-top:1.5rem; }
  .checks table { width:100%; border-collapse:collapse; font-size:.85rem; }
  .checks th { text-align:left; color:#94a3b8; padding:.3rem .5rem; border-bottom:1px solid #334155; }
  .checks td { padding:.3rem .5rem; border-bottom:1px solid #1e293b; }
</style>
</head>
<body>
<h1>ComfyUI Health Monitor &nbsp;<span id="status-badge">…</span></h1>
<div id="alerts"></div>
<div class="grid" id="cards"></div>
<div class="checks card"><h2>All Checks</h2><table id="check-table"><tr><th>Check</th><th>Status</th><th>Detail</th></tr></table></div>
<div id="ts"></div>
<script>
const STATUS_CLASS = {OK:'ok',WARN:'warn',CRITICAL:'crit',UNKNOWN:'unk'};
const STATUS_ICON  = {OK:'✅',WARN:'⚠️',CRITICAL:'❌',UNKNOWN:'?'};

function fmt_bytes(b){ if(!b) return '—'; return (b/(1024**3)).toFixed(1)+' GB'; }
function pct_class(p,w,c){ return p>=c?'val-crit':p>=w?'val-warn':'val-ok'; }

function card(title, rows){
  let html='<div class="card"><h2>'+title+'</h2>';
  rows.forEach(([k,v,cls])=>{ html+='<div class="row"><span>'+k+'</span><span class="'+(cls||'')+'">'+v+'</span></div>'; });
  return html+'</div>';
}

async function refresh(){
  try{
    const r=await fetch('/api/health');
    const d=await r.json();

    // Badge
    const b=document.getElementById('status-badge');
    b.textContent=d.overall_status;
    b.className=STATUS_CLASS[d.overall_status]||'unk';

    // Alerts
    const al=document.getElementById('alerts');
    al.innerHTML=d.alerts.map(a=>'<div class="alert">'+a+'</div>').join('');

    // Cards
    const cards=[];
    const sys=d.system_stats;
    if(sys){
      const rows=[];
      rows.push(['CPU', sys.cpu_utilization.toFixed(0)+'%', pct_class(sys.cpu_utilization,70,90)]);
      if(sys.ram_total){
        const rp=(sys.ram_used/sys.ram_total*100);
        rows.push(['RAM', fmt_bytes(sys.ram_used)+' / '+fmt_bytes(sys.ram_total)+' ('+rp.toFixed(0)+'%)', pct_class(rp,70,85)]);
      }
      (sys.devices||[]).filter(d=>d.type!=='cpu').forEach(dev=>{
        const used=dev.vram_total-dev.vram_free;
        const vp=dev.vram_total?used/dev.vram_total*100:0;
        rows.push([dev.name.substring(0,22), fmt_bytes(used)+' / '+fmt_bytes(dev.vram_total)+' ('+vp.toFixed(0)+'%)', pct_class(vp,90,97)]);
      });
      if(sys.disk_total_bytes){
        const dfree=sys.disk_free_bytes, dtotal=sys.disk_total_bytes;
        const dp=(dtotal-dfree)/dtotal*100;
        const dc=dfree<5368709120?'val-crit':dfree<21474836480?'val-warn':'val-ok'; // 5 GB / 20 GB
        rows.push(['Disk', fmt_bytes(dfree)+' free / '+fmt_bytes(dtotal)+' ('+dp.toFixed(0)+'% used)', dc]);
      }
      cards.push(card('System',rows));
    }

    const q=d.queue_stats;
    if(q){
      cards.push(card('Queue',[
        ['Running', q.running.length, q.running.length?'val-warn':'val-ok'],
        ['Pending', q.pending.length, q.pending.length>10?'val-crit':q.pending.length?'val-warn':'val-ok'],
      ]));
    }

    const nu=d.nunchaku;
    if(nu && nu.nodes_found && nu.nodes_found.length){
      cards.push(card('Nunchaku',[
        ['DiT Loader', nu.dit_loader_present?'✓':'✗', nu.dit_loader_present?'val-ok':'val-crit'],
        ['Text Encoder', nu.text_encoder_present?'✓':'✗', nu.text_encoder_present?'val-ok':'val-warn'],
        ['Precision', nu.precision_mode||'unknown', nu.precision_mode?'val-ok':''],
        ['FB Cache', nu.fb_cache_enabled?'ON':'OFF', nu.fb_cache_enabled?'val-ok':''],
      ]));
    }

    const gs=d.generation_stats;
    if(gs){
      cards.push(card('Gen Stats',[
        ['Error rate', gs.error_rate_pct.toFixed(0)+'%', pct_class(gs.error_rate_pct,1,20)],
        ['Total jobs', gs.total_jobs, ''],
      ]));
    }

    document.getElementById('cards').innerHTML=cards.join('');

    // Checks table
    const tb=document.getElementById('check-table');
    let rows='<tr><th>Check</th><th>Status</th><th>Detail</th></tr>';
    (d.checks||[]).forEach(c=>{
      rows+='<tr><td>'+c.name+'</td><td class="'+(STATUS_CLASS[c.status]||'')+'">'+STATUS_ICON[c.status]+' '+c.status+'</td><td>'+c.message+'</td></tr>';
    });
    tb.innerHTML=rows;

    document.getElementById('ts').textContent='Last updated: '+d.timestamp;
  }catch(e){
    document.getElementById('status-badge').textContent='ERROR';
    document.getElementById('status-badge').className='crit';
    console.error(e);
  }
}

refresh();
setInterval(refresh,5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return _HTML


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    try:
        return await _run_health_check()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _run_health_check() -> dict[str, Any]:
    """Run all health checks and return a serialised FullHealthReport.

    Mirrors cli.py's _collect_report exactly — any new check added there must
    also be added here.  The only intentional difference is that running_since
    is always None (single-shot; the server has no persistent watch state).
    """
    url        = _cfg.url
    thresholds = _cfg.thresholds
    history_jobs   = thresholds.get("history_jobs", 50)
    vram_warn      = thresholds.get("vram_warn_pct", 90)
    vram_crit      = thresholds.get("vram_critical_pct", 97)
    queue_warn     = thresholds.get("queue_warn", 10)
    stale_minutes  = thresholds.get("stale_job_minutes", 5)
    disk_warn_gb   = thresholds.get("disk_warn_gb", 20.0)
    disk_crit_gb   = thresholds.get("disk_critical_gb", 5.0)
    disk_warn_pct  = thresholds.get("disk_warn_pct", 90.0)
    disk_crit_pct  = thresholds.get("disk_critical_pct", 95.0)

    async with ComfyUIClient(url, timeout=_cfg.timeout) as client:
        raw = await client.fetch_all(history_jobs=history_jobs)

    report = FullHealthReport(
        comfyui_url=url,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    # 1. Connectivity — try alternate ports on failure (same as CLI)
    if raw["system_stats"] is None:
        alt_url = await probe_for_live_url(url, timeout=1.5)
        if alt_url:
            report.alerts.append(
                f"Note: configured URL {url} didn't respond; "
                f"auto-switched to {alt_url} — update your settings to avoid this"
            )
            report.comfyui_url = alt_url
            async with ComfyUIClient(alt_url, timeout=_cfg.timeout) as client2:
                raw = await client2.fetch_all(history_jobs=history_jobs)
            url = alt_url

    conn_check = check_connectivity(raw["system_stats"], url=url)
    report.checks.append(conn_check)
    if conn_check.status == HealthStatus.CRITICAL:
        report.overall_status = HealthStatus.CRITICAL
        report.alerts.append(conn_check.message)
        return report.model_dump()

    # 2. System stats
    report.system_stats = _parse_system_stats(raw["system_stats"])

    # 2b. Disk space
    disk_check, (disk_free, disk_total) = check_disk_space(
        warn_gb=disk_warn_gb,
        critical_gb=disk_crit_gb,
        warn_pct=disk_warn_pct,
        critical_pct=disk_crit_pct,
    )
    report.checks.append(disk_check)
    if report.system_stats and disk_total > 0:
        report.system_stats.disk_free_bytes = disk_free
        report.system_stats.disk_total_bytes = disk_total

    # 2c. Symlink awareness
    sym_check, _ = check_symlinks(
        warn_gb=disk_warn_gb,
        critical_gb=disk_crit_gb,
    )
    report.checks.append(sym_check)

    # 3. Nunchaku detection
    nunchaku = detect_nunchaku(raw["object_info"])
    report.nunchaku = nunchaku

    # 4. Queue health
    q_check, queue_stats = check_queue_health(raw["queue"], warn_threshold=queue_warn)
    report.checks.append(q_check)
    report.queue_stats = queue_stats

    # 5. Stale jobs (single-shot — no running_since state across requests)
    stale_check = check_stale_jobs(raw["queue"], stale_minutes=stale_minutes, running_since=None)
    report.checks.append(stale_check)

    # 6. VRAM
    vram_check, _ = check_vram_health(
        raw["system_stats"],
        warn_pct=vram_warn,
        critical_pct=vram_crit,
        nunchaku=nunchaku,
        nunchaku_anomaly_gb=thresholds.get("nunchaku_vram_anomaly_gb", 14.0),
        nunchaku_min_card_gb=thresholds.get("nunchaku_min_card_gb", 12.0),
    )
    report.checks.append(vram_check)

    # 7. RAM
    report.checks.append(check_ram_health(raw["system_stats"], warn_pct=thresholds.get("ram_warn_pct", 85)))

    # 8. Nunchaku nodes — gated by nunchaku_checks config flag
    if nunchaku.nodes_found and _cfg.get("nunchaku_checks", True):
        report.checks.append(check_nunchaku_nodes(nunchaku))

    # 9. Error rate + generation stats
    err_check, gen_stats = check_error_rate(raw["history"], history_jobs=history_jobs)
    report.checks.append(err_check)
    report.generation_stats = gen_stats

    # 10. Model files (via object_info)
    model_check = check_model_files(raw["object_info"], _cfg.expected_models)
    report.checks.append(model_check)

    # Aggregate alerts and overall status
    for check in report.checks:
        if check.status in (HealthStatus.WARN, HealthStatus.CRITICAL):
            report.alerts.append(check.message)

    statuses = [c.status for c in report.checks]
    if HealthStatus.CRITICAL in statuses:
        report.overall_status = HealthStatus.CRITICAL
    elif HealthStatus.WARN in statuses:
        report.overall_status = HealthStatus.WARN
    else:
        report.overall_status = HealthStatus.OK

    return report.model_dump()


def serve(host: str = "127.0.0.1", port: int = 8190, reload: bool = False) -> None:
    uvicorn.run("comfyui_health.dashboard_server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    serve()
