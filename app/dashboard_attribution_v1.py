from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import uvicorn
from fastapi import Depends

from app import attribution_core as attribution
from app import cfb_exact_position_fix_v3 as base
from app import dashboard_metrics_v3 as metrics
from app import nfl_capper_ingest as nfl
from app import sh01_account_reconcile
from app import termux_executor_dashboard_v2 as termux_v2
from app import universal_position_identity as identity

composite = base.composite
app = base.app
dashboard = composite.dashboard
core = composite.core

# Apply source attribution before any stats endpoint is called. This also hard
# moves executions that do not match the linked call into SH01 while retaining
# original_strategy_source for audit history.
attribution.repair_records(core)
attribution.install_stats_attribution(core)

# Sport-agnostic exact-position display. Every current/future sport uses the
# purchased outcome rather than an opponent-side line parsed from the question.
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
        row["strategy_source"] = rec.get("strategy_source")
        row["attribution_source"] = rec.get("attribution_source") or attribution.attribution_for_execution(rec)[0]
        row["attribution_reason"] = rec.get("attribution_reason")
        row["original_strategy_source"] = rec.get("original_strategy_source")
    return rows, total


dashboard._estimate_pnl = _estimate_pnl_universal
termux_v2._dashboard_exact_position = lambda rec, q: identity.canonical_exact_position(rec, q)

_ORIGINAL_MORE_STATS = metrics._more_stats_payload


def _collect_missed_units(executions: dict[str, Any]) -> dict[str, Any]:
    """Aggregate missed-call unit P/L by capper and sport.

    Missed-call grading currently exists for NFL/CFB Telegram feeds. Every stats
    row still receives a missed-unit field; unsupported monitors/sports show 0u
    until they expose a graded missed-signal feed.
    """
    specs = (
        (
            core.DATA_DIR / "nfl_capper_signals.json",
            ("Slam - NFL", "Syndicate - NFL"),
            "NFL",
            Decimal(os.getenv("NFL_CAPPER_UNIT_USDC", "10")),
        ),
        (
            core.DATA_DIR / "cfb_capper_preview_signals.json",
            ("Slam - CFB", "Syndicate - CFB"),
            "CFB",
            Decimal(os.getenv("CFB_CAPPER_UNIT_USDC", "10")),
        ),
    )
    by_capper: dict[str, dict[str, Decimal | int]] = {}
    by_sport: dict[str, dict[str, Decimal | int]] = {}
    for path, labels, sport, default_unit in specs:
        signals = core._load(path)
        if not isinstance(signals, dict):
            signals = {}
        unit_by_label: dict[str, Decimal] = {}
        for label in labels:
            try:
                unit_by_label[label] = Decimal(str(nfl._capper_unit_config(core, label, default_unit)["unit_usdc"]))
            except Exception:
                unit_by_label[label] = default_unit
        try:
            stats = nfl._missed_signal_stats(
                signals,
                executions,
                labels=labels,
                sport=sport,
                unit_usdc=default_unit,
                unit_usdc_by_label=unit_by_label,
            )
        except Exception:
            stats = {}
        sport_units = Decimal("0")
        sport_graded = 0
        sport_called = Decimal("0")
        for label in labels:
            row = stats.get(label) or {}
            units = attribution._d(row.get("missed_pnl_units"))
            called = attribution._d(row.get("missed_units_called"))
            graded = int(row.get("missed_graded") or 0)
            capper = metrics._capper_name(label, sport) or label
            bucket = by_capper.setdefault(capper, {"units": Decimal("0"), "graded": 0, "called": Decimal("0")})
            bucket["units"] = Decimal(str(bucket["units"])) + units
            bucket["graded"] = int(bucket["graded"]) + graded
            bucket["called"] = Decimal(str(bucket["called"])) + called
            sport_units += units
            sport_graded += graded
            sport_called += called
        by_sport[sport] = {"units": sport_units, "graded": sport_graded, "called": sport_called}
    return {"by_capper": by_capper, "by_sport": by_sport}


def _apply_missed_fields(row: dict[str, Any], bucket: dict[str, Any] | None) -> None:
    data = bucket or {}
    units = attribution._d(data.get("units"))
    called = attribution._d(data.get("called"))
    row["missed_units_pnl"] = str(units.quantize(Decimal("0.01")))
    row["missed_units_called"] = str(called.quantize(Decimal("0.01")))
    row["missed_graded"] = int(data.get("graded") or 0)


def _more_stats_with_units(mode: str = "live") -> dict[str, Any]:
    attribution.repair_records(core)
    payload = _ORIGINAL_MORE_STATS(mode)
    executions = core._load(core.EXECUTIONS_FILE)
    if not isinstance(executions, dict):
        executions = {}
    records = [
        rec for rec in executions.values()
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
    missed = _collect_missed_units(executions)

    capper_rows = payload.setdefault("by_capper", [])
    for row in capper_rows:
        name = str(row.get("name") or "")
        row.update(attribution.unit_summary(records, marks, source_name=name))
        _apply_missed_fields(row, (missed.get("by_capper") or {}).get(name))
        for sub in row.get("bet_types") or []:
            sub.update(attribution.unit_summary(records, marks, source_name=name, bet_type=str(sub.get("name") or "")))

    sport_rows = payload.setdefault("by_sport", [])
    for row in sport_rows:
        sport = str(row.get("name") or "")
        row.update(attribution.unit_summary(records, marks, sport=sport))
        _apply_missed_fields(row, (missed.get("by_sport") or {}).get(sport))
        for sub in row.get("bet_types") or []:
            sub.update(attribution.unit_summary(records, marks, sport=sport, bet_type=str(sub.get("name") or "")))

    for row in payload.get("by_bet_type") or []:
        row.update(attribution.unit_summary(records, marks, bet_type=str(row.get("name") or "")))
        _apply_missed_fields(row, None)

    # SH01 is always visible, even at 0 trades, so the user never has to infer
    # whether manual/unmatched account activity is being tracked.
    sh01 = next((r for r in capper_rows if str(r.get("name") or "").upper() == attribution.SH01), None)
    if sh01 is None:
        sh01 = {
            "name": attribution.SH01,
            "bets": 0, "open": 0, "wins": 0, "losses": 0, "pushes": 0,
            "stake_usdc": "0.00", "realized_pnl_usdc": "0.00",
            "realized_pnl_7d_usdc": "0.00", "realized_pnl_30d_usdc": "0.00",
            "unrealized_pnl_usdc": "0.00", "total_live_pnl_usdc": "0.00",
            "roi_pct": None, "win_pct": None, "sports": [], "bet_types": [],
        }
        sh01.update(attribution.unit_summary(records, marks, source_name=attribution.SH01))
        _apply_missed_fields(sh01, None)
        capper_rows.insert(0, sh01)
    else:
        sh01.update(attribution.unit_summary(records, marks, source_name=attribution.SH01))
        _apply_missed_fields(sh01, None)
        capper_rows[:] = [sh01] + [r for r in capper_rows if r is not sh01]

    payload["sh01"] = sh01
    payload["line_attribution_tolerance_points"] = str(attribution.LINE_TOLERANCE_POINTS)
    return payload


metrics._more_stats_payload = _more_stats_with_units


@app.get("/api/dashboard/attribution-stats", dependencies=[Depends(dashboard._auth)])
def attribution_stats(mode: str = "live"):
    return _more_stats_with_units(mode)


@app.post("/api/dashboard/reconcile-sh01", dependencies=[Depends(dashboard._auth)])
def reconcile_sh01_now():
    attribution.repair_records(core)
    return sh01_account_reconcile.reconcile(core)


# Make unit P/L and SH01 visible in the existing MORE STATS cards rather than
# relying on a hidden secondary panel. A compact SH01 summary remains below it.
html = dashboard.DASHBOARD_HTML
if "attributionStatsPanel" not in html:
    css = r'''
.attribution-stats-panel{margin-top:10px;border:2px inset #fff;background:#c0c0c0;padding:8px;color:#000}.attribution-stats-title{font-weight:900;margin-bottom:6px}.attribution-row{background:#000;color:#ddd;padding:7px 9px;margin:5px 0;font-family:monospace;line-height:1.5}.attribution-row b{color:#fff}.attribution-row.sh01{border-left:5px solid #f59e0b}.more-stats-row.sh01{border-left:5px solid #f59e0b}.unit-pos{color:#22c55e;font-weight:900}.unit-neg{color:#ef4444;font-weight:900}.unit-flat{color:#ddd;font-weight:900}.unit-line{font-family:monospace;font-weight:800}.missed-unit{color:#fbbf24;font-weight:900}
'''
    html = html.replace("</style>", css + "</style>", 1)
    js = r'''
function attrUnit(v){const n=Number(v||0),c=n>0?'unit-pos':n<0?'unit-neg':'unit-flat';return '<span class="'+c+'">'+(n>0?'+':'')+n.toFixed(2)+'u</span>'}
function attrMissedUnit(v){const n=Number(v||0);return '<span class="missed-unit">'+(n>0?'+':'')+n.toFixed(2)+'u</span>'}
function attrMoney(v){const n=Number(v||0);return (n>0?'+':'')+'$'+n.toFixed(2)}

// Override the base MORE STATS renderer so unit P/L is on every capper/monitor
// and sport card. Missed Unit P/L is hypothetical and is NOT added to Actual Total.
function moreStatsBetTypes(types){
 if(!Array.isArray(types)||!types.length)return '';
 return '<div class="more-stats-bet-types">'+types.map(row=>{
  const roi=row.roi_pct===null||row.roi_pct===undefined?'—':Number(row.roi_pct).toFixed(1)+'%';
  return '<div class="more-stats-bet-type"><b>'+moreStatsEsc(row.name)+'</b> Bets '+Number(row.bets||0)
   +' · W-L-P '+Number(row.wins||0)+'-'+Number(row.losses||0)+'-'+Number(row.pushes||0)
   +' · ROI '+roi
   +' · Actual '+attrUnit(row.total_live_units_pnl)
   +' · <span class="pnl '+moreStatsPnlClass(row.realized_pnl_usdc)+'">'+moreStatsMoney(row.realized_pnl_usdc)+'</span></div>';
 }).join('')+'</div>';
}
function renderMoreStats(){
 const body=document.getElementById('moreStatsBody');
 const scope=document.getElementById('moreStatsScope');
 if(!body||!moreStatsData)return;
 const mode=String(moreStatsData.mode||'live').toUpperCase();
 if(scope)scope.textContent=mode+' trades · 1-point linked-call tolerance · SH01 separated automatically';
 document.querySelectorAll('[data-more-stats-tab]').forEach(btn=>btn.classList.toggle('active',btn.dataset.moreStatsTab===moreStatsTab));
 const rows=moreStatsTab==='sport'
  ? (moreStatsData.by_sport||[])
  : moreStatsTab==='bet_type'
  ? (moreStatsData.by_bet_type||[])
  : (moreStatsData.by_capper||[]);
 if(!rows.length){body.innerHTML='<div style="margin-top:12px;opacity:.7">No tracked trades in this scope.</div>';return;}
 body.innerHTML='<div class="more-stats-grid">'+rows.map(row=>{
  const win=row.win_pct===null||row.win_pct===undefined?'—':Number(row.win_pct).toFixed(1)+'%';
  const roi=row.roi_pct===null||row.roi_pct===undefined?'—':Number(row.roi_pct).toFixed(1)+'%';
  const related=moreStatsTab==='sport'?(row.cappers||[]).join(', '):moreStatsTab==='bet_type'?[...(row.sports||[]),...(row.cappers||[])].join(', '):(row.sports||[]).join(', ');
  const betTypes=moreStatsTab==='bet_type'?'':moreStatsBetTypes(row.bet_types||[]);
  const isSh=String(row.name||'').toUpperCase()==='SH01';
  return '<div class="more-stats-row '+(isSh?'sh01':'')+'"><div class="name">'+moreStatsEsc(row.name)+'</div>'
   +'<div>'+(related?moreStatsEsc(related)+' · ':'')+'Bets '+Number(row.bets||0)+' · Open '+Number(row.open||0)+'</div>'
   +'<div>W-L-P '+Number(row.wins||0)+'-'+Number(row.losses||0)+'-'+Number(row.pushes||0)+' · Win '+win+'</div>'
   +'<div>Stake $'+Number(row.stake_usdc||0).toFixed(2)+' · ROI '+roi+'</div>'
   +'<div class="pnl '+moreStatsPnlClass(row.realized_pnl_usdc)+'">Realized P/L '+moreStatsMoney(row.realized_pnl_usdc)+'</div>'
   +'<div><span class="pnl '+moreStatsPnlClass(row.realized_pnl_7d_usdc)+'">7D '+moreStatsMoney(row.realized_pnl_7d_usdc)+'</span> · <span class="pnl '+moreStatsPnlClass(row.realized_pnl_30d_usdc)+'">30D '+moreStatsMoney(row.realized_pnl_30d_usdc)+'</span></div>'
   +'<div class="pnl '+moreStatsPnlClass(row.total_live_pnl_usdc)+'">Live P/L '+moreStatsMoney(row.total_live_pnl_usdc)+'</div>'
   +'<div class="unit-line">Units called '+Number(row.units_called||0).toFixed(2)+'u · Realized '+attrUnit(row.realized_units_pnl)+' · Live '+attrUnit(row.live_units_pnl)+'</div>'
   +'<div class="unit-line">Actual Unit P/L '+attrUnit(row.total_live_units_pnl)+' · Missed Unit P/L '+attrMissedUnit(row.missed_units_pnl)+' <small>('+Number(row.missed_graded||0)+' missed)</small></div>'
   +betTypes+'</div>';
 }).join('')+'</div>';
}

function renderAttributionStats(d){
 const host=document.getElementById('attributionStatsRows');if(!host)return;
 const rows=Array.isArray(d.by_capper)?d.by_capper:[];
 const sh=rows.find(x=>String(x.name||'').toUpperCase()==='SH01')||d.sh01;
 if(!sh){host.innerHTML='<div class="attribution-row sh01"><b>SH01</b> · no trades yet</div>';return;}
 const roi=sh.roi_pct==null?'—':Number(sh.roi_pct).toFixed(1)+'%';
 host.innerHTML='<div class="attribution-row sh01"><b>SH01</b> · Bets '+Number(sh.bets||0)+' · Open '+Number(sh.open||0)
  +' · W-L-P '+Number(sh.wins||0)+'-'+Number(sh.losses||0)+'-'+Number(sh.pushes||0)
  +' · Actual Unit P/L '+attrUnit(sh.total_live_units_pnl)+' · P/L '+attrMoney(sh.total_live_pnl_usdc)+' · ROI '+roi+'</div>';
}
async function loadAttributionStats(){try{const mode=localStorage.getItem('dashboardStatsFilter')||'live';const r=await fetch('/api/dashboard/attribution-stats?mode='+encodeURIComponent(mode),{cache:'no-store'});if(r.ok)renderAttributionStats(await r.json())}catch(e){}}
function installAttributionStatsPanel(){
 const parent=document.getElementById('moreStatsPanel');if(!parent||document.getElementById('attributionStatsPanel'))return;
 const p=document.createElement('div');p.id='attributionStatsPanel';p.className='attribution-stats-panel';
 p.innerHTML='<div class="attribution-stats-title">SH01 · UNMATCHED / MANUAL ACCOUNT BETS</div><div style="font-size:12px;margin-bottom:5px">Outside the linked capper/monitor call or more than 1 point from its line. Original source is retained only for audit history.</div><div id="attributionStatsRows"></div>';
 parent.appendChild(p);loadAttributionStats();setInterval(loadAttributionStats,5000);
}
if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',installAttributionStatsPanel)}else{installAttributionStatsPanel()}
'''
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html

sh01_account_reconcile.start_background(core, 30)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
