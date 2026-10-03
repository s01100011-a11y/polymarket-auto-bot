from __future__ import annotations

import os
from typing import Any

import uvicorn
from fastapi import Depends

from app import attribution_core as attribution
from app import cfb_exact_position_fix_v3 as base
from app import dashboard_metrics_v3 as metrics
from app import sh01_account_reconcile
from app import termux_executor_dashboard_v2 as termux_v2
from app import universal_position_identity as identity

composite = base.composite
app = base.app
dashboard = composite.dashboard
core = composite.core

# Apply source attribution before any stats endpoint is called.
attribution.repair_records(core)
attribution.install_stats_attribution(core)

# Sport-agnostic exact-position display. This is final in the wrapper chain, so
# every current/future sport uses the purchased outcome rather than a named
# opponent's spread from the market question.
_ORIGINAL_ESTIMATE = dashboard._estimate_pnl

def _estimate_pnl_universal(records: list[dict[str, Any]]):
    rows, total = _ORIGINAL_ESTIMATE(records)
    by_id = {str(r.get("id") or ""): r for r in records if isinstance(r, dict) and r.get("id")}
    for row in rows:
        if not isinstance(row, dict):
            continue
        rec = by_id.get(str(row.get("id") or ""))
        if not isinstance(rec, dict):
            continue
        exact = identity.canonical_exact_position(rec)
        if exact:
            row["exact_position"] = exact
        row["attribution_source"] = rec.get("attribution_source") or attribution.attribution_for_execution(rec)[0]
    return rows, total

dashboard._estimate_pnl = _estimate_pnl_universal
termux_v2._dashboard_exact_position = lambda rec, q: identity.canonical_exact_position(rec, q)

_ORIGINAL_MORE_STATS = metrics._more_stats_payload

def _more_stats_with_units(mode: str = "live") -> dict[str, Any]:
    attribution.repair_records(core)
    payload = _ORIGINAL_MORE_STATS(mode)
    executions = core._load(core.EXECUTIONS_FILE)
    records = [
        rec for rec in (executions.values() if isinstance(executions, dict) else [])
        if isinstance(rec, dict)
        and not rec.get("parent_trade_id")
        and (
            mode == "both"
            or (mode == "paper" and bool(rec.get("paper")))
            or (mode not in {"paper", "both"} and not bool(rec.get("paper")))
        )
    ]
    try:
        current, _ = dashboard._estimate_pnl(records)
    except Exception:
        current = []
    marks = {str(r.get("id")): r for r in current if isinstance(r, dict) and r.get("id")}

    for row in payload.get("by_capper") or []:
        name = str(row.get("name") or "")
        row.update(attribution.unit_summary(records, marks, source_name=name))
        for sub in row.get("bet_types") or []:
            sub.update(attribution.unit_summary(records, marks, source_name=name, bet_type=str(sub.get("name") or "")))
    for row in payload.get("by_sport") or []:
        sport = str(row.get("name") or "")
        row.update(attribution.unit_summary(records, marks, sport=sport))
        for sub in row.get("bet_types") or []:
            sub.update(attribution.unit_summary(records, marks, sport=sport, bet_type=str(sub.get("name") or "")))
    for row in payload.get("by_bet_type") or []:
        row.update(attribution.unit_summary(records, marks, bet_type=str(row.get("name") or "")))

    sh01 = next((r for r in payload.get("by_capper") or [] if str(r.get("name") or "").upper() == attribution.SH01), None)
    payload["sh01"] = sh01 or {
        "name": attribution.SH01,
        "bets": 0, "open": 0, "wins": 0, "losses": 0, "pushes": 0,
        "stake_usdc": "0.00", "realized_pnl_usdc": "0.00",
        "unrealized_pnl_usdc": "0.00", "total_live_pnl_usdc": "0.00",
        "roi_pct": None, "units_called": "0.00", "realized_units_pnl": "0.00",
        "live_units_pnl": "0.00", "total_live_units_pnl": "0.00",
        "unit_pnl_coverage_bets": 0, "sports": [],
    }
    return payload

metrics._more_stats_payload = _more_stats_with_units

@app.get("/api/dashboard/attribution-stats", dependencies=[Depends(dashboard._auth)])
def attribution_stats(mode: str = "live"):
    return _more_stats_with_units(mode)

@app.post("/api/dashboard/reconcile-sh01", dependencies=[Depends(dashboard._auth)])
def reconcile_sh01_now():
    return sh01_account_reconcile.reconcile(core)

# Dedicated Stats view. Existing capper/sport panels continue to work, but their
# backend now excludes SH01 trades through the attribution-aware stats function.
html = dashboard.DASHBOARD_HTML
if "attributionStatsPanel" not in html:
    css = r'''
.attribution-stats-panel{margin-top:10px;border:2px inset #fff;background:#c0c0c0;padding:8px;color:#000}.attribution-stats-title{font-weight:900;margin-bottom:6px}.attribution-row{background:#000;color:#ddd;padding:7px 9px;margin:5px 0;font-family:monospace;line-height:1.5}.attribution-row b{color:#fff}.attribution-row.sh01{border-left:5px solid #f59e0b}.unit-pos{color:#22c55e;font-weight:900}.unit-neg{color:#ef4444;font-weight:900}.unit-flat{color:#ddd;font-weight:900}
'''
    html = html.replace("</style>", css + "</style>", 1)
    js = r'''
function attrUnit(v){const n=Number(v||0),c=n>0?'unit-pos':n<0?'unit-neg':'unit-flat';return '<span class="'+c+'">'+(n>0?'+':'')+n.toFixed(2)+'u</span>'}
function attrMoney(v){const n=Number(v||0);return (n>0?'+':'')+'$'+n.toFixed(2)}
function renderAttributionStats(d){
 const host=document.getElementById('attributionStatsRows');if(!host)return;
 const rows=Array.isArray(d.by_capper)?d.by_capper:[];
 const sh=rows.find(x=>String(x.name||'').toUpperCase()==='SH01');
 const ordered=sh?[sh,...rows.filter(x=>x!==sh)]:rows;
 host.innerHTML=ordered.length?ordered.map(x=>{
  const n=String(x.name||'Unknown'),isSh=n.toUpperCase()==='SH01',roi=x.roi_pct==null?'—':Number(x.roi_pct).toFixed(1)+'%';
  return '<div class="attribution-row '+(isSh?'sh01':'')+'"><b>'+n+'</b> · '+((x.sports||[]).join(', ')||'—')
   +' · Bets '+Number(x.bets||0)+' · W-L-P '+Number(x.wins||0)+'-'+Number(x.losses||0)+'-'+Number(x.pushes||0)
   +' · Units called '+Number(x.units_called||0).toFixed(2)+'u'
   +' · Realized '+attrUnit(x.realized_units_pnl)+' · Live '+attrUnit(x.live_units_pnl)+' · Total '+attrUnit(x.total_live_units_pnl)
   +' · P/L '+attrMoney(x.total_live_pnl_usdc)+' · ROI '+roi
   +' · Unit coverage '+Number(x.unit_pnl_coverage_bets||0)+'</div>';
 }).join(''):'<div class="attribution-row">No attributed trades yet.</div>';
}
async function loadAttributionStats(){try{const r=await fetch('/api/dashboard/attribution-stats?mode=live');if(r.ok)renderAttributionStats(await r.json())}catch(e){}}
function installAttributionStatsPanel(){
 const parent=document.getElementById('moreStatsPanel');if(!parent||document.getElementById('attributionStatsPanel'))return;
 const p=document.createElement('div');p.id='attributionStatsPanel';p.className='attribution-stats-panel';
 p.innerHTML='<div class="attribution-stats-title">CAPPER / MONITOR UNIT P&L + SH01</div><div style="font-size:12px;margin-bottom:5px">SH01 = live dashboard/account bets that are not an exact linked original capper/monitor call.</div><div id="attributionStatsRows"></div>';
 parent.appendChild(p);loadAttributionStats();setInterval(loadAttributionStats,5000);
}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',installAttributionStatsPanel)}else{installAttributionStatsPanel()}
'''
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html

sh01_account_reconcile.start_background(core, 30)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
