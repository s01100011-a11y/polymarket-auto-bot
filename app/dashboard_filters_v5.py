from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import Depends, Query

from app import dashboard_live_control_v4 as base

app = base.app
dashboard = base.dashboard
core = base.core
metrics_base = base.base


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _trade_bucket(rec: dict[str, Any]) -> str | None:
    if rec.get("parent_trade_id"):
        return None
    if bool(rec.get("paper")):
        return "paper"

    status = str(rec.get("status") or "").upper()
    if "DRY_RUN" in status or status in {"FAILED", "REJECTED", "CANCELLED"}:
        return None

    source = str(rec.get("source") or "")
    execution = rec.get("execution") or {}
    placed = bool(execution.get("placed"))

    # LIVE means a real funded execution, regardless of whether it originated
    # from Slack/PW automation or the Termux executor path. Failed/pending
    # diagnostics never reach this bucket.
    if source in {"slack_live", "termux_executor"} and (
        placed
        or status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "CLOSED", "CLOSED_RECONCILED"}
    ):
        return "live"
    return None


def _performance(mode: str) -> dict[str, Any]:
    executions = core._load(core.EXECUTIONS_FILE)
    wins = losses = pushes = graded = 0
    open_trades = 0
    realized_total = Decimal("0")
    stake_total = Decimal("0")

    for rec in executions.values():
        bucket = _trade_bucket(rec)
        if bucket is None:
            continue
        if mode != "both" and bucket != mode:
            continue
        if rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
            open_trades += 1
            continue

        realized = metrics_base.base.pnl_base._explicit_realized_pnl(rec)
        if realized is None:
            continue

        stake = _d(rec.get("budget_usdc"))
        realized_total += realized
        if stake > 0:
            stake_total += stake
        graded += 1

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1
        else:
            pushes += 1

    decided = wins + losses
    accuracy = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
    roi = (realized_total / stake_total * Decimal("100")) if stake_total > 0 else None

    return {
        "mode": mode,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "graded_trades": graded,
        "open_trades": open_trades,
        "graded_stake_usdc": str(stake_total.quantize(Decimal("0.01"))),
        "realized_pnl": str(realized_total.quantize(Decimal("0.01"))),
        "accuracy_pct": str(accuracy.quantize(Decimal("0.1"))) if accuracy is not None else None,
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
    }


@app.get("/api/dashboard/performance", dependencies=[Depends(dashboard._auth)])
def dashboard_performance(
    mode: str = Query(default="both", pattern="^(both|paper|live)$"),
):
    return _performance(mode)


def _install_filters() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="statsModeFilter"' in html:
        return

    # dashboard_metrics_v3 writes unfiltered stats during every normal dashboard
    # refresh. Remove that block here so it cannot race/overwrite the selected
    # BOTH/PAPER/LIVE view. Filtered stats are rendered only by
    # refreshFilteredStats() below.
    metric_overwrite = """  const roi=document.getElementById('performanceRoi'),wl=document.getElementById('performanceWL'),acc=document.getElementById('performanceAccuracy'),push=document.getElementById('performancePushes'),graded=document.getElementById('performanceGraded');
  if(roi){const n=Number(s.roi_pct);roi.textContent=s.roi_pct===null||s.roi_pct===undefined?'—':n.toFixed(1)+'%';roi.className='performance-value '+(n>0?'green':n<0?'red':'')}
  if(wl)wl.textContent=String(s.wins||0)+' / '+String(s.losses||0);
  if(push)push.textContent=(s.pushes||0)+' push'+((s.pushes||0)===1?'':'es');
  if(acc){const a=Number(s.accuracy_pct);acc.textContent=s.accuracy_pct===null||s.accuracy_pct===undefined?'—':a.toFixed(1)+'%'}
  if(graded)graded.textContent=(s.graded_trades||0)+' graded closed trades';"""
    html = html.replace(metric_overwrite, "")

    stats_filter = """
  <div class="mode-filter-row" id="statsModeFilter">
    <span class="mode-filter-label">Stats</span>
    <button type="button" class="view-filter-btn" data-stats-filter="both">BOTH</button>
    <button type="button" class="view-filter-btn" data-stats-filter="paper">PAPER</button>
    <button type="button" class="view-filter-btn" data-stats-filter="live">LIVE</button>
  </div>
"""
    html = html.replace(
        '  <div class="performance-strip">',
        stats_filter + '  <div class="performance-strip">',
        1,
    )

    trade_filter = """
    <div class="mode-filter-row current-filter-row" id="openTradesModeFilter">
      <span class="mode-filter-label">Open trades</span>
      <button type="button" class="view-filter-btn" data-trades-filter="both">BOTH</button>
      <button type="button" class="view-filter-btn" data-trades-filter="paper">PAPER</button>
      <button type="button" class="view-filter-btn" data-trades-filter="live">LIVE</button>
    </div>
"""
    current_anchor = (
        '<h2>Current watches & trades</h2><p class="note">Price watches refresh in the bot loop. '
        'Submitted live trades show estimated mark-to-market P/L.</p>'
    )
    html = html.replace(current_anchor, current_anchor + trade_filter, 1)

    css = """
.mode-filter-row{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:14px 0 4px}
.current-filter-row{margin:12px 0 14px}
.mode-filter-label{font-size:10px;color:var(--muted);font-weight:900;letter-spacing:.09em;text-transform:uppercase;margin-right:3px}
.view-filter-btn{border:1px solid #344157;background:#101827;color:#aebbd0;border-radius:999px;padding:6px 11px;font-size:10px;font-weight:900;letter-spacing:.04em;cursor:pointer}
.view-filter-btn.active{background:#263652;border-color:#60779e;color:#fff}
.view-filter-btn[data-stats-filter="paper"].active,.view-filter-btn[data-trades-filter="paper"].active{background:#3b2d0d;border-color:#8a6717;color:#fde68a}
.view-filter-btn[data-stats-filter="live"].active,.view-filter-btn[data-trades-filter="live"].active{background:#15382f;border-color:#2f7d66;color:#a7f3d0}
"""
    html = html.replace("</style>", css + "</style>", 1)

    js = r"""
const dashboardStatsDefaultVersion='live-v1';
if(localStorage.getItem('dashboardStatsDefaultVersion')!==dashboardStatsDefaultVersion){
 localStorage.setItem('dashboardStatsFilter','live');
 localStorage.setItem('dashboardStatsDefaultVersion',dashboardStatsDefaultVersion);
}
let dashboardStatsFilter=localStorage.getItem('dashboardStatsFilter')||'live';
let dashboardTradesFilter=localStorage.getItem('dashboardTradesFilter')||'both';

function filterButtons(){
 document.querySelectorAll('[data-stats-filter]').forEach(function(b){
  b.classList.toggle('active',b.dataset.statsFilter===dashboardStatsFilter);
 });
 document.querySelectorAll('[data-trades-filter]').forEach(function(b){
  b.classList.toggle('active',b.dataset.tradesFilter===dashboardTradesFilter);
 });
}

async function refreshFilteredStats(){
 try{
  const r=await fetch('/api/dashboard/performance?mode='+encodeURIComponent(dashboardStatsFilter),{cache:'no-store'});
  const s=await r.json();
  if(!r.ok)throw new Error(s.detail||'Stats filter failed');
  const roi=document.getElementById('performanceRoi');
  const wl=document.getElementById('performanceWL');
  const acc=document.getElementById('performanceAccuracy');
  const push=document.getElementById('performancePushes');
  const graded=document.getElementById('performanceGraded');
  if(roi){
   const n=Number(s.roi_pct);
   roi.textContent=s.roi_pct===null||s.roi_pct===undefined?'—':n.toFixed(1)+'%';
   roi.className='performance-value '+(n>0?'green':n<0?'red':'');
  }
  if(wl)wl.textContent=String(s.wins||0)+' / '+String(s.losses||0);
  if(push)push.textContent=(s.pushes||0)+' push'+((s.pushes||0)===1?'':'es');
  if(acc){
   const a=Number(s.accuracy_pct);
   acc.textContent=s.accuracy_pct===null||s.accuracy_pct===undefined?'—':a.toFixed(1)+'%';
  }
  if(graded)graded.textContent=(s.graded_trades||0)+' graded closed · '+String(s.open_trades||0)+' open · '+String(s.mode||dashboardStatsFilter).toUpperCase();
 }catch(e){}
}

function applyOpenTradesFilter(){
 const body=document.getElementById('currentBody');
 if(!body)return;
 body.querySelectorAll('tbody tr').forEach(function(row){
  const badge=row.querySelector('td:first-child .status');
  if(!badge)return;
  const status=(badge.textContent||'').trim().toUpperCase();
  if(status!=='PAPER'&&status!=='LIVE'&&status!=='PARTIAL')return;
  const isPaper=status==='PAPER';
  const show=dashboardTradesFilter==='both'||
    (dashboardTradesFilter==='paper'&&isPaper)||
    (dashboardTradesFilter==='live'&&!isPaper);
  row.style.display=show?'':'none';
 });
}

document.addEventListener('click',function(ev){
 const sb=ev.target.closest('[data-stats-filter]');
 if(sb){
  dashboardStatsFilter=sb.dataset.statsFilter||'both';
  localStorage.setItem('dashboardStatsFilter',dashboardStatsFilter);
  filterButtons();
  refreshFilteredStats();
  return;
 }
 const tb=ev.target.closest('[data-trades-filter]');
 if(tb){
  dashboardTradesFilter=tb.dataset.tradesFilter||'both';
  localStorage.setItem('dashboardTradesFilter',dashboardTradesFilter);
  filterButtons();
  applyOpenTradesFilter();
 }
});

filterButtons();
refreshFilteredStats();
setInterval(refreshFilteredStats,5000);

const currentTradesObserver=new MutationObserver(function(){applyOpenTradesFilter()});
const currentTradesBody=document.getElementById('currentBody');
if(currentTradesBody){
 currentTradesObserver.observe(currentTradesBody,{childList:true,subtree:true});
 applyOpenTradesFilter();
}
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_filters()
