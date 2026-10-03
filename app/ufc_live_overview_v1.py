from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from fastapi import Depends, Query

from app import ufc_live_page_v1 as live
from app import ufc_rebuy_approval_v1 as _rebuy_approval  # noqa: F401

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
