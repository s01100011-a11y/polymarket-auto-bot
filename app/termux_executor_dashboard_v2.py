from __future__ import annotations

from decimal import Decimal
from typing import Any

from app import termux_executor_dashboard as base
from app import slack_dashboard_v2 as pnl_base

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.ingest


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _estimate_pnl_live_v2(records: list[dict]) -> tuple[list[dict], Decimal]:
    """Keep partially closed live trades visible and mark only remaining shares."""
    current: list[dict] = []
    total = Decimal("0")
    active = [r for r in records if r.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}]
    if not active:
        return current, total

    try:
        client = ingest.PublicClient()
    except Exception:
        client = None

    try:
        for rec in active:
            q = rec.get("quote") or {}
            asset_id = q.get("asset_id")
            shares = _d(rec.get("remaining_shares") if rec.get("remaining_shares") is not None else q.get("shares"))
            entry = _d(q.get("paper_entry_price") or q.get("entry_price") or q.get("limit_price"))
            live_price = None
            midpoint = None
            pnl = None
            price_error = None

            if client and asset_id and shares > 0 and entry > 0:
                try:
                    live_price = _d(client.get_price(asset_id=str(asset_id), side="SELL"))
                    midpoint = _d(client.get_midpoint(asset_id=str(asset_id)))
                    pnl = (live_price - entry) * shares
                    total += pnl
                except Exception as exc:
                    price_error = str(exc)
                    try:
                        midpoint = _d(client.get_midpoint(asset_id=str(asset_id)))
                        pnl = (midpoint - entry) * shares
                        total += pnl
                        live_price = midpoint
                    except Exception:
                        live_price = None
                        midpoint = None
                        pnl = None

            current.append({
                "id": rec.get("id"),
                "market": q.get("market") or (rec.get("intent") or {}).get("market_url"),
                "market_url": q.get("market_url") or (rec.get("intent") or {}).get("market_url"),
                "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
                "side": pnl_base._trade_side(rec),
                "entry_price": str(entry) if entry else None,
                "current_price": str(live_price) if live_price is not None else None,
                "current_sell_price": str(live_price) if live_price is not None else None,
                "current_midpoint": str(midpoint) if midpoint is not None else None,
                "shares": str(shares) if shares else None,
                "budget_usdc": rec.get("budget_usdc"),
                "estimated_pnl": str(pnl.quantize(Decimal("0.01"))) if pnl is not None else None,
                "pnl_type": "unrealized" if pnl is not None else None,
                "submitted_at": rec.get("submitted_at") or rec.get("created_at"),
                "order_id": (rec.get("execution") or {}).get("order_id"),
                "status": rec.get("status"),
                "source": rec.get("source"),
                "paper": bool(rec.get("paper")),
                "price_error": price_error,
            })
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
    return current, total


# The original dashboard snapshot calls dashboard._estimate_pnl dynamically.
dashboard._estimate_pnl = _estimate_pnl_live_v2


def _install_per_trade_sell_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if "data-live-sell" in html:
        return

    old_lr = "const lr=d.live_trades.map(x=>`<tr><td><span class=\"status\">${x.paper?'PAPER':'LIVE'}</span></td><td class=\"${String(x.side||'BUY').toUpperCase()==='SELL'?'side-sell':'side-buy'}\">${esc(x.side||'BUY')}</td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.entry_price)}</td><td>${price(x.current_price||x.current_sell_price||x.current_midpoint)}</td><td>${money(x.budget_usdc)}</td><td class=\"${Number(x.estimated_pnl)>0?'green':Number(x.estimated_pnl)<0?'red':''}\">${money(x.estimated_pnl)}<span class=\"pnl-sub\">live</span></td><td>${when(x.submitted_at)}</td></tr>`);"
    new_lr = "const lr=d.live_trades.map(x=>`<tr><td><span class=\"status\">${x.paper?'PAPER':(x.status==='PARTIALLY_CLOSED'?'PARTIAL':'LIVE')}</span></td><td class=\"${String(x.side||'BUY').toUpperCase()==='SELL'?'side-sell':'side-buy'}\">${esc(x.side||'BUY')}</td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.entry_price)}</td><td>${price(x.current_price||x.current_sell_price||x.current_midpoint)}</td><td>${money(x.budget_usdc)}</td><td class=\"${Number(x.estimated_pnl)>0?'green':Number(x.estimated_pnl)<0?'red':''}\">${money(x.estimated_pnl)}<span class=\"pnl-sub\">live</span></td><td>${when(x.submitted_at)}</td><td>${x.paper?`<button class=\"paper-close-btn\" data-paper-close=\"${esc(x.id||'')}\">CLOSE</button>`:((['railway_executor','termux_executor','slack_live'].includes(x.source))?`<button class=\"live-sell-btn\" data-live-sell=\"${esc(x.id||'')}\">SELL</button>`:'—')}</td></tr>`);"
    html = html.replace(old_lr, new_lr)

    old_current = "table(['Status','Side','Market','Outcome','Entry','Live price','Budget','Live P/L','Submitted'],lr);renderPnlChart(d.pnl_history||[]);"
    new_current = "table(['Status','Side','Market','Outcome','Entry','Live price','Budget','Live P/L','Submitted','Action'],lr);renderPnlChart(d.pnl_history||[]);"
    html = html.replace(old_current, new_current)

    css = '''
.live-sell-btn{border:1px solid #7f1d1d;background:#3f1118;color:#fecdd3;border-radius:8px;padding:7px 11px;font-size:11px;font-weight:850;cursor:pointer;white-space:nowrap}.live-sell-btn:hover{background:#571621}.live-sell-btn:disabled{opacity:.55;cursor:wait}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
document.addEventListener('click',async function(ev){
 const btn=ev.target.closest('[data-live-sell]');
 if(!btn)return;
 const tradeId=btn.getAttribute('data-live-sell');
 if(!tradeId)return;
 if(!remoteExecutorConnected){alert('Railway direct execution is not ready.');return}
 if(!confirm('SELL this live position now directly from Railway?'))return;
 const original=btn.textContent;
 try{
  btn.disabled=true;btn.textContent='SELLING…';
  const q=await remoteRequest('/api/executor/request-sell/'+encodeURIComponent(tradeId));
  const d=await waitRemote(q.request_id);
  btn.textContent=d.status==='PARTIALLY_CLOSED'?'PARTIAL':'SOLD';
  setTimeout(()=>load(),500);
 }catch(e){
  btn.disabled=false;btn.textContent='RETRY SELL';
  alert('SELL failed: '+String(e));
  setTimeout(()=>load(),500);
 }
});
'''
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_per_trade_sell_ui()
