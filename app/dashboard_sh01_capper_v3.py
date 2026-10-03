from __future__ import annotations

import os
from typing import Any

import uvicorn
from fastapi import Depends

from app import attribution_core as attribution
from app import dashboard_attribution_v1 as base
from app import nfl_capper_ingest as nfl

app = base.app
dashboard = base.dashboard
core = base.core

_KNOWN_SPORTS = ("NFL", "CFB", "WNBA", "NBA")


def _strong_sport_from_record(rec: dict[str, Any]) -> str | None:
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    settlement = rec.get("settlement") if isinstance(rec.get("settlement"), dict) else {}
    text = " ".join(
        str(value or "")
        for value in (
            quote.get("market_url"),
            quote.get("market"),
            settlement.get("market_slug"),
            rec.get("market_slug"),
            rec.get("event_slug"),
        )
    ).casefold()
    # WNBA must be checked before NBA because "wnba" contains "nba".
    tests = (
        ("WNBA", ("/wnba/", "/event/wnba-", " wnba-", "wnba-")),
        ("NBA", ("/nba/", "/event/nba-", " nba-", "nba-")),
        ("CFB", ("/cfb/", "/event/cfb-", " cfb-", "cfb-", "ncaaf")),
        ("NFL", ("/nfl/", "/event/nfl-", " nfl-", "nfl-")),
    )
    for sport, needles in tests:
        if any(needle in text for needle in needles):
            return sport
    return None


def _record_sport(rec: dict[str, Any]) -> str | None:
    strong = _strong_sport_from_record(rec)
    if strong:
        return strong
    direct = str(rec.get("strategy_sport") or "").strip().upper()
    return direct if direct in _KNOWN_SPORTS else None


def _sh01_execution_view(executions: dict[str, Any]) -> dict[str, Any]:
    """Return only genuine SH01 rows, with strict sport routing.

    This deliberately excludes paper/test/duplicate audit rows.  It also treats
    the Polymarket market URL/slug as stronger sport evidence than a stale
    strategy_sport tag, preventing NFL rows from leaking into the CFB card.
    """
    scoped: dict[str, Any] = {}
    for key, raw in executions.items():
        if not isinstance(raw, dict) or raw.get("parent_trade_id"):
            continue
        if raw.get("paper") or raw.get("stats_excluded"):
            continue
        source = str(
            raw.get("attribution_source")
            or raw.get("strategy_source")
            or attribution.attribution_for_execution(raw)[0]
            or ""
        ).strip().upper()
        if source != attribution.SH01:
            continue
        sport = _record_sport(raw)
        if sport not in _KNOWN_SPORTS:
            continue
        rec = dict(raw)
        rec["strategy_source"] = attribution.SH01
        rec["attribution_source"] = attribution.SH01
        rec["strategy_sport"] = sport
        scoped[key] = rec
    return scoped


def _sh01_cappers_payload() -> dict[str, Any]:
    attribution.repair_records(core)
    raw = core._load(core.EXECUTIONS_FILE)
    executions = raw if isinstance(raw, dict) else {}
    scoped = _sh01_execution_view(executions)
    records = [rec for rec in scoped.values() if isinstance(rec, dict)]
    try:
        current, _ = dashboard._estimate_pnl(records)
    except Exception:
        current = []
    live_marks = {
        str(row.get("id")): row
        for row in current
        if isinstance(row, dict) and row.get("id")
    }

    out: dict[str, Any] = {}
    for sport in _KNOWN_SPORTS:
        stats = nfl._stats_from_executions(
            scoped,
            labels=(attribution.SH01,),
            sport=sport,
            live_marks=live_marks,
        ).get(attribution.SH01, {})
        stats = dict(stats or {})
        stats.update(
            {
                "name": f"SH01 - {sport}",
                "label": attribution.SH01,
                "sport": sport,
                "manual": True,
                "enabled": True,
                "positions": nfl._position_items(
                    scoped,
                    attribution.SH01,
                    sport=sport,
                    limit=80,
                    live_marks=live_marks,
                ),
                "signals": 0,
                "all_items": [],
            }
        )
        out[sport] = stats

    return {
        "capper": attribution.SH01,
        "sports": out,
        "line_attribution_tolerance_points": str(attribution.LINE_TOLERANCE_POINTS),
    }


@app.get("/api/dashboard/sh01-cappers", dependencies=[Depends(dashboard._auth)])
def sh01_cappers() -> dict[str, Any]:
    return _sh01_cappers_payload()


html = dashboard.DASHBOARD_HTML
if "sh01CapperRuntimeV3" not in html:
    css = r'''
.sh01-capper-card{border-left:5px solid #f59e0b!important}.sh01-unit-main{font-weight:900}.sh01-position-note{font-size:11px;margin:5px 0 8px;opacity:.78}.sh01-card-empty{margin-top:7px;opacity:.7}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
const sh01CapperRuntimeV3=true;

function installAttributionStatsPanel(){}

function sh01Esc(v){return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot',"'":'&#39;'}[ch]));}
function sh01Money(v){if(v===null||v===undefined||v==='')return '—';const n=Number(v||0);return (n>0?'+':'')+'$'+n.toFixed(2)}
function sh01PnlClass(v){const n=Number(v||0);return n>0?'positive':n<0?'negative':'flat'}
function sh01Unit(v){const n=Number(v||0);return (n>0?'+':'')+n.toFixed(2)+'u'}
function sh01Odds(v){if(v===null||v===undefined||v==='')return '—';const p=Number(v);if(!Number.isFinite(p)||p<=0||p>=1)return '—';const c=p*100,ct=Math.abs(c-Math.round(c))<0.05?String(Math.round(c)):c.toFixed(1);return (1/p).toFixed(2)+' ('+ct+'¢)'}
function sh01Units(v){if(v===null||v===undefined||v==='')return '';const n=Number(v);if(!Number.isFinite(n))return String(v)+'U';return n.toFixed(2).replace(/\.00$/,'').replace(/(\.\d)0$/,'$1')+'U'}
function sh01Time(v){if(!v)return '—';const d=new Date(v);return Number.isNaN(d.getTime())?sh01Esc(v):sh01Esc(d.toLocaleString())}

function sh01SetMainUnitMetric(containerId,value){
 const host=document.getElementById(containerId);if(!host)return;
 const grid=host.querySelector('.capper-metrics-grid');if(!grid)return;
 let box=grid.querySelector('[data-main-unit-pnl]');
 if(!box){box=document.createElement('div');box.className='capper-metric';box.dataset.mainUnitPnl='1';box.innerHTML='<span>Unit P/L</span><b class="capper-pnl sh01-unit-main">—</b>';grid.appendChild(box)}
 const b=box.querySelector('b'),n=Number(value||0);if(!b)return;
 b.textContent=sh01Unit(n);b.className='capper-pnl sh01-unit-main '+sh01PnlClass(n);
}
function sh01DecorateMainCards(){
 try{
  if(typeof nflLastCappers!=='undefined'){
   sh01SetMainUnitMetric('nflCapperSlam',(nflLastCappers['Slam - NFL']||{}).total_live_units_pnl);
   sh01SetMainUnitMetric('nflCapperSyndicate',(nflLastCappers['Syndicate - NFL']||{}).total_live_units_pnl);
  }
  if(typeof cfbLastSources!=='undefined'){
   sh01SetMainUnitMetric('cfbCapperSlam',((cfbLastSources['Slam - CFB']||{}).performance||{}).total_live_units_pnl);
   sh01SetMainUnitMetric('cfbCapperSyndicate',((cfbLastSources['Syndicate - CFB']||{}).performance||{}).total_live_units_pnl);
  }
  if(typeof basketballMonitorData!=='undefined'){
   sh01SetMainUnitMetric('wnbaMonitorCard',(basketballMonitorData['WNBA Monitor - WNBA']||{}).total_live_units_pnl);
   sh01SetMainUnitMetric('nbaMonitorCard',(basketballMonitorData['NBA Monitor - NBA']||{}).total_live_units_pnl);
  }
 }catch(e){}
}
if(typeof nflRenderCappers==='function'){const _nflRenderCappers=nflRenderCappers;nflRenderCappers=function(){const r=_nflRenderCappers.apply(this,arguments);sh01DecorateMainCards();return r}}
if(typeof cfbRenderCappers==='function'){const _cfbRenderCappers=cfbRenderCappers;cfbRenderCappers=function(){const r=_cfbRenderCappers.apply(this,arguments);sh01DecorateMainCards();return r}}
if(typeof renderBasketballMonitors==='function'){const _renderBasketballMonitors=renderBasketballMonitors;renderBasketballMonitors=function(){const r=_renderBasketballMonitors.apply(this,arguments);sh01DecorateMainCards();return r}}

function sh01Metrics(x){
 const win=x.win_pct===null||x.win_pct===undefined?'—':Number(x.win_pct).toFixed(1)+'%';
 const roi=x.roi_pct===null||x.roi_pct===undefined?'—':Number(x.roi_pct).toFixed(1)+'%';
 const openValue=x.open_value_usdc===null||x.open_value_usdc===undefined?'—':'$'+Number(x.open_value_usdc).toFixed(2);
 return '<div class="capper-metrics-grid">'
  +'<div class="capper-metric"><span>Bets</span><b>'+Number(x.bets||0)+'</b></div>'
  +'<div class="capper-metric"><span>Open</span><b>'+Number(x.open||0)+'</b></div>'
  +'<div class="capper-metric"><span>W-L-P</span><b>'+Number(x.wins||0)+'-'+Number(x.losses||0)+'-'+Number(x.pushes||0)+'</b></div>'
  +'<div class="capper-metric"><span>Win</span><b>'+win+'</b></div>'
  +'<div class="capper-metric"><span>Stake</span><b>$'+Number(x.graded_stake_usdc||0).toFixed(2)+'</b></div>'
  +'<div class="capper-metric"><span>ROI</span><b>'+roi+'</b></div>'
  +'<div class="capper-metric"><span>Realized P/L</span><b class="capper-pnl '+sh01PnlClass(x.realized_pnl_usdc)+'">'+sh01Money(x.realized_pnl_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>Live P/L</span><b class="capper-pnl '+sh01PnlClass(x.total_live_pnl_usdc)+'">'+sh01Money(x.total_live_pnl_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>Unit P/L</span><b class="capper-pnl '+sh01PnlClass(x.total_live_units_pnl)+'">'+sh01Unit(x.total_live_units_pnl)+'</b></div>'
  +'<div class="capper-metric"><span>7D P/L</span><b class="capper-pnl '+sh01PnlClass(x.realized_pnl_7d_usdc)+'">'+sh01Money(x.realized_pnl_7d_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>30D P/L</span><b class="capper-pnl '+sh01PnlClass(x.realized_pnl_30d_usdc)+'">'+sh01Money(x.realized_pnl_30d_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>Open value</span><b>'+openValue+'</b></div>'
  +'</div>';
}

function sh01PositionRows(x){
 const all=Array.isArray(x.positions)?x.positions:[];
 const open=all.filter(v=>v.sell_available);
 const settledAll=all.filter(v=>!v.sell_available&&v.finished);
 const settled=(typeof capperLast24hOnly!=='undefined'&&capperLast24hOnly)?settledAll.filter(v=>capperWithin24h(v.closed_at||v.submitted_at)):settledAll;
 const renderOpen=v=>{
  const units=sh01Units(v.units),unitTitle=units?' · '+units:'';
  const exact=v.exact_position?'<div><b>Exact position:</b> '+sh01Esc(v.exact_position)+'</div>':'';
  const target=v.target_profit_usdc!==null&&v.target_profit_usdc!==undefined&&v.target_profit_usdc!==''?'<div><b>To win $'+Number(v.target_profit_usdc).toFixed(2)+'</b>'+(units?' ('+units+')':'')+'</div>':'';
  const shares=v.shares===null||v.shares===undefined||v.shares===''?'—':Number(v.shares).toFixed(2);
  const stake=v.open_cost_basis_usdc||v.stake_usdc;
  const stakeText=stake===null||stake===undefined||stake===''?'—':'$'+Number(stake).toFixed(2);
  const value=v.current_value_usdc===null||v.current_value_usdc===undefined?'—':'$'+Number(v.current_value_usdc).toFixed(2);
  const sell=v.trade_id?'<button type="button" style="margin-top:6px" data-trade-id="'+sh01Esc(v.trade_id)+'" onclick="sh01SellPosition(this.dataset.tradeId,this)">SELL POSITION</button>':'';
  return '<div class="nfl-position-row open"><div class="nfl-position-title">'+sh01Esc(v.selection||v.market||v.exact_position||'SH01 position')+unitTitle+' · OPEN</div>'+exact+target+'<div>'+sh01Esc(v.outcome||'')+' · Stake '+stakeText+' · Shares '+shares+'</div><div>Entry odds '+sh01Odds(v.entry_price)+' · Live odds '+sh01Odds(v.current_price)+' · Value '+value+'</div><div class="nfl-position-pnl '+sh01PnlClass(v.live_pnl_usdc)+'">Live P/L '+sh01Money(v.live_pnl_usdc)+'</div>'+sell+'</div>';
 };
 const openHtml=open.length?open.map(renderOpen).join(''):'<div class="sh01-card-empty">No open positions.</div>';
 let html='<div style="margin-top:9px"><span class="capper-section-badge">Open positions</span>'+openHtml+'</div>';
 if(settled.length||(typeof capperLast24hOnly!=='undefined'&&capperLast24hOnly)){
  const settledHtml=settled.length?settled.map(v=>{
   const result=String(v.result||'').toUpperCase();
   const cls=result==='WIN'?'win':result==='LOSS'?'loss':result==='PUSH'?'push':'';
   const awaiting=!result&&String(v.status||'').toUpperCase()==='CLOSED_RECONCILED';
   const units=sh01Units(v.units),unitTitle=units?' · '+units:'';
   const statusLabel=result?' · '+result:(awaiting?' · AWAITING SETTLEMENT':'');
   const exact=v.exact_position?'<div><b>Exact position:</b> '+sh01Esc(v.exact_position)+'</div>':'';
   const target=v.target_profit_usdc!==null&&v.target_profit_usdc!==undefined&&v.target_profit_usdc!==''?'<div><b>To win $'+Number(v.target_profit_usdc).toFixed(2)+'</b>'+(units?' ('+units+')':'')+'</div>':'';
   const shares=v.shares===null||v.shares===undefined||v.shares===''?'—':Number(v.shares).toFixed(2);
   const stake=v.stake_usdc===null||v.stake_usdc===undefined||v.stake_usdc===''?'—':'$'+Number(v.stake_usdc).toFixed(2);
   const entry='<div>Entry odds '+sh01Odds(v.entry_price)+' · Closed '+sh01Time(v.closed_at||v.submitted_at)+'</div>';
   const pnl=v.realized_pnl_usdc===null||v.realized_pnl_usdc===undefined?'':'<div class="nfl-position-pnl '+sh01PnlClass(v.realized_pnl_usdc)+'">Realized P/L '+sh01Money(v.realized_pnl_usdc)+'</div>';
   const manual=awaiting&&v.trade_id?'<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:5px"><button type="button" data-result="WIN" data-trade-id="'+sh01Esc(v.trade_id)+'" onclick="sh01Settle(this.dataset.tradeId,this.dataset.result,this)">SETTLE WIN</button><button type="button" data-result="LOSS" data-trade-id="'+sh01Esc(v.trade_id)+'" onclick="sh01Settle(this.dataset.tradeId,this.dataset.result,this)">SETTLE LOSS</button><button type="button" data-result="PUSH" data-trade-id="'+sh01Esc(v.trade_id)+'" onclick="sh01Settle(this.dataset.tradeId,this.dataset.result,this)">SETTLE PUSH</button></div>':'';
   return '<div class="nfl-position-row '+cls+'"><div class="nfl-position-title">'+sh01Esc(v.selection||v.market||v.exact_position||'SH01 position')+unitTitle+statusLabel+'</div>'+exact+target+'<div>'+sh01Esc(v.outcome||'')+' · Stake '+stake+' · Shares '+shares+'</div>'+entry+pnl+manual+'</div>';
  }).join(''):'<div class="sh01-card-empty">No settled positions in the last 24 hours.</div>';
  html+='<div style="margin-top:12px"><span class="capper-section-badge">Settled positions'+((typeof capperLast24hOnly!=='undefined'&&capperLast24hOnly)?' · last 24h':'')+'</span>'+settledHtml+'</div>';
 }
 return html;
}

function sh01CardBody(x){
 return '<div class="sh01-position-note">Manual/dashboard/account bets attributed to SH01. No automatic signal feed.</div>'+sh01Metrics(x)+sh01PositionRows(x);
}
function sh01PanelSport(panel){
 if(!panel)return '';
 const title=panel.querySelector('.capper-panel-title');
 const m=String(title&&title.textContent||'').trim().toUpperCase().match(/^([A-Z0-9]+)\s+AUTO-TRADING/);
 return m?m[1]:'';
}
function sh01EnsureCard(panel,sport,row){
 if(!panel)return;
 const grid=panel.querySelector('.nfl-capper-grid');if(!grid)return;
 const id='sh01CapperCard-'+sport,bodyId='sh01CapperBody-'+sport;
 let card=document.getElementById(id);
 if(!card){
  card=document.createElement('div');card.id=id;card.className='nfl-capper-card sh01-capper-card';
  card.innerHTML='<div class="capper-card-head"><b>SH01 - '+sh01Esc(sport)+'</b><span class="capper-event-badge">MANUAL</span></div><div class="nfl-capper-kpis" id="'+bodyId+'">—</div>';
  grid.appendChild(card);
 }
 const body=document.getElementById(bodyId);if(body)body.innerHTML=sh01CardBody(row||{});
}
function renderSh01Cappers(data){
 const rows=(data&&data.sports)||{};
 document.querySelectorAll('.nfl-capper-panel').forEach(panel=>{
  const sport=sh01PanelSport(panel);if(!sport)return;
  if(rows[sport])sh01EnsureCard(panel,sport,rows[sport]);
 });
 sh01DecorateMainCards();
}
async function loadSh01Cappers(){
 try{const r=await fetch('/api/dashboard/sh01-cappers',{cache:'no-store'}),d=await r.json();if(r.ok)renderSh01Cappers(d)}catch(e){}
}
async function sh01SellPosition(tradeId,btn){
 if(!confirm('Sell the full tracked SH01 position at the current executable market?'))return;
 const old=btn.textContent;btn.disabled=true;btn.textContent='SELLING…';
 try{
  const r=await fetch('/api/executor/request-sell/'+encodeURIComponent(tradeId),{method:'POST'}),q=await r.json();if(!r.ok)throw new Error(q.detail||'SELL request failed');
  btn.textContent='QUEUED';setTimeout(loadSh01Cappers,1200);
 }catch(e){btn.disabled=false;btn.textContent=old;alert(String(e.message||e))}
}
async function sh01Settle(tradeId,result,btn){
 if(!confirm('Manually settle this SH01 position as '+result+'?'))return;
 const old=btn.textContent;btn.disabled=true;btn.textContent='SETTLING…';
 try{const r=await fetch('/api/dashboard/manual-settle/'+encodeURIComponent(tradeId)+'/'+encodeURIComponent(result),{method:'POST'}),d=await r.json();if(!r.ok)throw new Error(d.detail||'Settlement failed');await loadSh01Cappers()}catch(e){btn.disabled=false;btn.textContent=old;alert(String(e.message||e))}
}

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>{sh01DecorateMainCards();loadSh01Cappers();});else{sh01DecorateMainCards();loadSh01Cappers();}
setInterval(loadSh01Cappers,10000);
'''
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
