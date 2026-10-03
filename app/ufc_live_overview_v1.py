from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from fastapi import Depends, Query

from app import ufc_live_page_v1 as live
from app import ufc_rebuy_approval_v1 as _rebuy_approval  # noqa: F401
from app import ufc_sh01_dashboard as ufc

app = live.app
dashboard = live.dashboard
core = live.core
remote = live.remote

_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_SECONDS = 1.0


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _position_text(rec: dict[str, Any]) -> str:
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            rec.get("strategy_execution_selection"),
            rec.get("strategy_selection"),
            rec.get("exact_position"),
            rec.get("outcome"),
            quote.get("resolved_outcome"),
            quote.get("requested_outcome"),
            quote.get("outcome"),
            rec.get("market"),
        )
    )


def _overview(fight_id: str | None) -> dict[str, Any]:
    key = str(fight_id or "")
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])

    state = remote._state()
    cash = _d(state.get("usdc_balance"))
    positions_value = _d(state.get("portfolio_value"))
    portfolio_value = cash + positions_value

    raw = core._load(core.EXECUTIONS_FILE)
    executions = raw if isinstance(raw, dict) else {}
    active: list[dict[str, Any]] = []
    for rec in executions.values():
        if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("paper"):
            continue
        if str(rec.get("status") or "").upper() not in _ACTIVE:
            continue
        active.append(rec)

    try:
        marked, _ = dashboard._estimate_pnl(active)
    except Exception:
        marked = []
    marks = {
        str(row.get("id") or ""): row
        for row in marked
        if isinstance(row, dict)
    }

    portfolio_live = Decimal("0")
    card_live = Decimal("0")
    fight_live = Decimal("0")
    card_open = 0
    fight_open = 0

    fighters = ufc.FIGHTS.get(str(fight_id or ""))
    fighter_pnl: dict[str, Decimal] = {
        name: Decimal("0") for name in (fighters or ())
    }
    fighter_positions: dict[str, int] = {
        name: 0 for name in (fighters or ())
    }

    for rec in active:
        trade_id = str(rec.get("id") or "")
        mark = marks.get(trade_id) or {}
        pnl = _d(
            mark.get("estimated_pnl")
            if mark.get("estimated_pnl") is not None
            else rec.get("estimated_pnl")
        )
        portfolio_live += pnl

        strategy_sport = str(rec.get("strategy_sport") or "").upper()
        pick_id = str(rec.get("strategy_pick_id") or "")
        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        market_url = str(quote.get("market_url") or rec.get("market_url") or "").lower()
        is_current_ufc = (
            strategy_sport == "UFC"
            or pick_id.startswith("ufc332:")
            or "/ufc/" in market_url
        )
        if not is_current_ufc:
            continue

        card_open += 1
        card_live += pnl
        if fight_id and live._fight_id_from_payload(rec) == fight_id:
            fight_open += 1
            fight_live += pnl
            if fighters:
                text = ufc._compact(_position_text(rec))
                for fighter in fighters:
                    if ufc._compact(fighter) in text:
                        fighter_pnl[fighter] += pnl
                        fighter_positions[fighter] += 1
                        break

    result = {
        "ok": True,
        "portfolio_value_usdc": str(portfolio_value.quantize(Decimal("0.01"))),
        "available_usdc": str(cash.quantize(Decimal("0.01"))),
        "positions_value_usdc": str(positions_value.quantize(Decimal("0.01"))),
        "portfolio_live_pnl_usdc": str(portfolio_live.quantize(Decimal("0.01"))),
        "card_total_pnl_usdc": str(card_live.quantize(Decimal("0.01"))),
        "active_fight_pnl_usdc": str(fight_live.quantize(Decimal("0.01"))),
        "card_open_positions": card_open,
        "active_fight_open_positions": fight_open,
        "fight_id": fight_id,
        "fighters": [
            {
                "name": fighter,
                "live_pnl_usdc": str(fighter_pnl[fighter].quantize(Decimal("0.01"))),
                "open_positions": fighter_positions[fighter],
            }
            for fighter in (fighters or ())
        ],
    }
    _CACHE[key] = (now, result)
    return dict(result)


@app.get("/api/ufc-live/overview", dependencies=[Depends(dashboard._auth)])
def ufc_live_overview(fight_id: str | None = Query(default=None)) -> dict[str, Any]:
    return _overview(fight_id)


html = live.UFC_LIVE_HTML
if "ufc-live-overview-v1" not in html:
    html = html.replace(
        '<div class="summary"><div class="box"><b>POSITION VALUE</b><span id="posValue">$0.00</span></div><div class="box"><b>LIVE P/L</b><span id="totalPnl">$0.00</span></div><div class="box"><b>ACTIVE FIGHT</b><span id="activeName">—</span></div></div>',
        '<div class="summary ufc-overview"><div class="box"><b>PORTFOLIO VALUE</b><span id="portfolioValue">$0.00</span></div><div class="box"><b>PORTFOLIO LIVE P/L</b><span id="portfolioLivePnl">$0.00</span></div><div class="box"><b>CARD TOTAL P/L</b><span id="cardTotalPnl">$0.00</span></div><div class="box"><b>ACTIVE FIGHT P/L</b><span id="activeFightPnl">$0.00</span></div></div>',
        1,
    )
    html = html.replace(
        '.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:0 6px 6px}',
        '.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;padding:0 6px 6px}',
        1,
    )
    html = html.replace(
        "$('activeName').textContent=f.fighter_a+' vs '+f.fighter_b;",
        "",
        1,
    )
    html = html.replace(
        "</style>",
        r'''
.fighter-pnl-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:8px 0 3px}
.fighter-pnl-box{border:1px solid #666;background:#080808;color:#fff;padding:6px;text-align:center;font-weight:900;line-height:1.35}
.fighter-pnl-box .fighter-name{display:block;font-size:11px;color:#ddd}
.fighter-pnl-box .fighter-pnl{display:block;font-size:15px;margin-top:2px}
.fighter-pnl-box .fighter-count{display:block;font-size:9px;color:#999;margin-top:1px}
@media(max-width:560px){.fighter-pnl-grid{grid-template-columns:1fr 1fr;gap:5px}.fighter-pnl-box{padding:5px}.fighter-pnl-box .fighter-pnl{font-size:14px}}
</style>''',
        1,
    )

    js = r'''
<script id="ufc-live-overview-v1">
(function(){
 let overviewBusy=false,overviewTimer=null;
 function signed(el,v){
  if(!el)return;
  const n=Number(v||0);
  el.textContent=(n>0?'+':'')+'$'+n.toFixed(2);
  el.className=n>0?'profit':n<0?'loss':'';
 }
 function renderFighterPnl(rows){
  const fight=document.querySelector('#fightHost .fight');
  if(!fight)return;
  const prices=fight.querySelector('.prices');
  if(!prices)return;
  let grid=fight.querySelector('.fighter-pnl-grid');
  if(!grid){
   grid=document.createElement('div');
   grid.className='fighter-pnl-grid';
   prices.parentNode.insertBefore(grid,prices);
  }
  const list=Array.isArray(rows)?rows:[];
  grid.innerHTML=list.map(x=>{
   const n=Number(x.live_pnl_usdc||0),cls=n>0?'profit':n<0?'loss':'',count=Number(x.open_positions||0);
   return '<div class="fighter-pnl-box"><span class="fighter-name">'+esc(x.name||'Fighter')+' · TOTAL P/L</span><span class="fighter-pnl '+cls+'">'+(n>0?'+':'')+'$'+n.toFixed(2)+'</span><span class="fighter-count">'+count+' open position'+(count===1?'':'s')+'</span></div>';
  }).join('');
 }
 async function refreshOverview(){
  if(overviewBusy||document.hidden){overviewTimer=setTimeout(refreshOverview,1500);return}
  overviewBusy=true;
  try{
   const f=typeof current==='function'?current():null;
   const u='/api/ufc-live/overview'+(f?.fight_id?('?fight_id='+encodeURIComponent(f.fight_id)):'');
   const r=await fetch(u,{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||r.status);
   const pv=document.getElementById('portfolioValue');
   if(pv)pv.textContent='$'+Number(d.portfolio_value_usdc||0).toFixed(2);
   signed(document.getElementById('portfolioLivePnl'),d.portfolio_live_pnl_usdc);
   signed(document.getElementById('cardTotalPnl'),d.card_total_pnl_usdc);
   signed(document.getElementById('activeFightPnl'),d.active_fight_pnl_usdc);
   renderFighterPnl(d.fighters);
  }catch(e){}finally{
   overviewBusy=false;
   overviewTimer=setTimeout(refreshOverview,1500);
  }
 }
 const oldChoose=window.choose;
 if(typeof oldChoose==='function')window.choose=function(id){const x=oldChoose(id);setTimeout(refreshOverview,40);return x};
 const oldSetTab=window.setTab;
 if(typeof oldSetTab==='function')window.setTab=function(x){const y=oldSetTab(x);setTimeout(refreshOverview,40);return y};
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshOverview()});
 setTimeout(refreshOverview,80);
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = html
