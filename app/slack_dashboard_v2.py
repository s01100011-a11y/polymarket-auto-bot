from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from app import slack_wnba as base

app = base.app
ingest = base.ingest
dashboard = ingest.dashboard
core = ingest.core

PNL_HISTORY_FILE = core.DATA_DIR / "pnl_history.json"
PNL_HISTORY_MAX_ENTRIES = 5760
PNL_SAMPLE_SECONDS = 10
_PNL_LOCK = threading.Lock()


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _trade_side(rec: dict[str, Any]) -> str:
    execution = rec.get("execution") or {}
    would_place = execution.get("would_place") or {}
    quote = rec.get("quote") or {}
    side = (
        rec.get("side")
        or execution.get("side")
        or would_place.get("side")
        or quote.get("trade_side")
        or quote.get("side")
    )
    if side:
        return str(side).upper()
    # Every currently implemented execution path enters a position with a BUY.
    return "BUY"


def _explicit_realized_pnl(rec: dict[str, Any]) -> Decimal | None:
    execution = rec.get("execution") or {}
    for value in (rec.get("realized_pnl"), execution.get("realized_pnl")):
        if value is not None:
            try:
                return Decimal(str(value))
            except Exception:
                pass

    quote = rec.get("quote") or {}
    entry = rec.get("entry_price") or quote.get("paper_entry_price") or quote.get("entry_price")
    exit_price = rec.get("exit_price") or execution.get("exit_price") or quote.get("exit_price")
    shares = rec.get("shares") or quote.get("shares")
    if entry is None or exit_price is None or shares is None:
        return None
    try:
        return (Decimal(str(exit_price)) - Decimal(str(entry))) * Decimal(str(shares))
    except Exception:
        return None


def _estimate_pnl_live(records: list[dict]) -> tuple[list[dict], Decimal]:
    """Final live estimator: reconcile fills first, then mark at executable SELL price."""
    current, _ = ingest._estimate_pnl_with_paper(records)
    total = Decimal("0")
    if not current:
        return current, total

    records_by_id = {str(r.get("id")): r for r in records if r.get("id") is not None}

    try:
        client = ingest.PublicClient()
    except Exception:
        client = None

    try:
        for row in current:
            rec = records_by_id.get(str(row.get("id"))) or {}
            q = rec.get("quote") or {}
            asset_id = str(q.get("asset_id") or "")
            shares = _d(row.get("shares"))
            entry = _d(row.get("entry_price"))
            cost = _d(row.get("budget_usdc"))
            if cost <= 0 and shares > 0 and entry > 0:
                cost = shares * entry

            live_price = None
            midpoint = _d(row.get("current_midpoint")) if row.get("current_midpoint") is not None else None
            price_error = None
            if client and asset_id:
                try:
                    # Use executable exit-side SELL price for mark-to-market P/L.
                    live_price = _d(client.get_price(asset_id=asset_id, side="SELL"))
                    try:
                        midpoint = _d(client.get_midpoint(asset_id=asset_id))
                    except Exception:
                        pass
                except Exception as exc:
                    price_error = str(exc)
                    if midpoint is not None and midpoint > 0:
                        live_price = midpoint

            pnl = None
            current_value = None
            pnl_percent = None
            if live_price is not None and live_price > 0 and shares > 0 and cost > 0:
                current_value = live_price * shares
                pnl = current_value - cost
                pnl_percent = pnl / cost * Decimal("100")
                total += pnl

            row["side"] = _trade_side(rec)
            row["current_price"] = str(live_price) if live_price is not None else row.get("current_midpoint")
            row["current_sell_price"] = str(live_price) if live_price is not None else None
            row["current_midpoint"] = str(midpoint) if midpoint is not None else row.get("current_midpoint")
            row["current_value_usdc"] = (
                str(current_value.quantize(Decimal("0.01"))) if current_value is not None else row.get("current_value_usdc")
            )
            row["estimated_pnl"] = (
                str(pnl.quantize(Decimal("0.01"))) if pnl is not None else row.get("estimated_pnl")
            )
            row["pnl_percent"] = (
                str(pnl_percent.quantize(Decimal("0.01"))) if pnl_percent is not None else row.get("pnl_percent")
            )
            row["pnl_type"] = "unrealized" if row.get("estimated_pnl") is not None else None
            row["paper"] = bool(rec.get("paper"))
            row["price_error"] = price_error

        # If a row could not be marked at SELL but the reconciler already produced
        # a P/L, include that fallback rather than dropping it from the portfolio.
        if total == 0:
            total = sum((_d(r.get("estimated_pnl")) for r in current), Decimal("0"))
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    return current, total


# Replace the prior midpoint-based estimator with executable exit-price marking.
dashboard._estimate_pnl = _estimate_pnl_live
_BASE_SNAPSHOT = dashboard._dashboard_snapshot


def _pnl_history(total_pnl: Decimal, open_trades: int) -> list[dict[str, Any]]:
    now = time.time()
    with _PNL_LOCK:
        history = core._load(PNL_HISTORY_FILE)
        latest_epoch = 0.0
        if history:
            try:
                latest_epoch = max(float(v.get("epoch") or 0) for v in history.values())
            except Exception:
                latest_epoch = 0.0

        if not history or now - latest_epoch >= PNL_SAMPLE_SECONDS:
            key = str(int(now * 1000))
            history[key] = {
                "epoch": now,
                "at": ingest._now_iso(),
                "pnl": str(total_pnl.quantize(Decimal("0.01"))),
                "open_trades": open_trades,
            }
            if len(history) > PNL_HISTORY_MAX_ENTRIES:
                ordered = sorted(history.items(), key=lambda kv: float((kv[1] or {}).get("epoch") or 0))
                history = dict(ordered[-PNL_HISTORY_MAX_ENTRIES:])
            core._save(PNL_HISTORY_FILE, history)

        points = list(history.values())
        points.sort(key=lambda x: float(x.get("epoch") or 0))
        # Keep the live dashboard payload compact while retaining a full day on disk.
        return points[-720:]


def _dashboard_snapshot_v2() -> dict[str, Any]:
    data = _BASE_SNAPSHOT()
    execution_map = core._load(core.EXECUTIONS_FILE)
    by_id = {str(k): v for k, v in execution_map.items()}
    current_by_id = {str(x.get("id")): x for x in data.get("live_trades", []) if x.get("id") is not None}

    realized_total = Decimal("0")
    for rec in execution_map.values():
        realized = _explicit_realized_pnl(rec)
        if realized is not None and rec.get("status") not in {"ORDER_SUBMITTED", "PAPER_OPEN"}:
            realized_total += realized

    for row in data.get("history", []):
        rec = by_id.get(str(row.get("id"))) or {}
        row["side"] = _trade_side(rec)
        live = current_by_id.get(str(row.get("id")))
        if live:
            row["pnl"] = live.get("estimated_pnl")
            row["pnl_type"] = "unrealized"
            row["live_price"] = live.get("current_price")
            row["trade_price"] = live.get("entry_price") or row.get("limit_price")
        else:
            realized = _explicit_realized_pnl(rec)
            row["pnl"] = str(realized.quantize(Decimal("0.01"))) if realized is not None else None
            row["pnl_type"] = "realized" if realized is not None else None
            row["live_price"] = None
            row["trade_price"] = (
                rec.get("exit_price")
                or (rec.get("execution") or {}).get("exit_price")
                or row.get("limit_price")
            )

    open_pnl = _d((data.get("status") or {}).get("estimated_total_pnl"))
    portfolio_pnl = open_pnl + realized_total
    data.setdefault("status", {})["estimated_total_pnl"] = str(portfolio_pnl.quantize(Decimal("0.01")))
    data["status"]["open_unrealized_pnl"] = str(open_pnl.quantize(Decimal("0.01")))
    data["status"]["realized_pnl"] = str(realized_total.quantize(Decimal("0.01")))
    data["status"]["pnl_note"] = "Live P/L marks open positions at the current SELL/exit price when available; realized P/L is included when a closed record contains exit data."
    data["pnl_history"] = _pnl_history(portfolio_pnl, len(data.get("live_trades", [])))
    return data


dashboard._dashboard_snapshot = _dashboard_snapshot_v2


def _install_trading_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="pnlChart"' in html:
        return

    chart_html = '''
    <div class="pnl-chart-card">
      <div class="pnl-chart-head"><div><div class="label">Live portfolio P/L</div><div class="pnl-chart-value" id="pnlChartValue">$0.00</div></div><div class="pnl-chart-range" id="pnlChartRange">Waiting for samples…</div></div>
      <div id="pnlChart" class="pnl-chart"><div class="empty">Collecting live P/L samples…</div></div>
    </div>
'''
    html = html.replace('    <div id="currentBody"></div>', chart_html + '    <div id="currentBody"></div>')

    css = '''
.pnl-chart-card{border:1px solid var(--border);background:#0d1522;border-radius:12px;padding:14px;margin:0 0 18px}.pnl-chart-head{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;margin-bottom:8px}.pnl-chart-value{font-size:24px;font-weight:850;margin-top:4px}.pnl-chart-range{font-size:11px;color:var(--muted);text-align:right}.pnl-chart{height:225px;position:relative;overflow:hidden}.pnl-chart svg{width:100%;height:100%;display:block}.pnl-line{fill:none;stroke:currentColor;stroke-width:3;vector-effect:non-scaling-stroke}.pnl-zero{stroke:#43516a;stroke-width:1;stroke-dasharray:5 5;vector-effect:non-scaling-stroke}.pnl-fill{fill:currentColor;opacity:.08}.pnl-axis{fill:var(--muted);font-size:22px}.side-buy{color:var(--accent);font-weight:850}.side-sell{color:var(--bad);font-weight:850}.pnl-sub{display:block;color:var(--muted);font-size:10px;margin-top:3px;text-transform:uppercase;letter-spacing:.05em}
'''
    html = html.replace('</style>', css + '</style>')

    chart_js = r'''
function renderPnlChart(points){
 const box=document.getElementById('pnlChart'),val=document.getElementById('pnlChartValue'),range=document.getElementById('pnlChartRange');
 if(!box||!val||!range)return;
 const pts=(points||[]).filter(x=>Number.isFinite(Number(x.pnl)));
 if(!pts.length){box.innerHTML='<div class="empty">Collecting live P/L samples…</div>';val.textContent='$0.00';range.textContent='Waiting for samples…';return}
 const values=pts.map(x=>Number(x.pnl));const last=values[values.length-1];val.textContent=money(last);val.className='pnl-chart-value '+(last>0?'green':last<0?'red':'');
 const start=new Date(pts[0].at),end=new Date(pts[pts.length-1].at);range.textContent=start.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+' → '+end.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+' · '+pts.length+' samples';
 const W=1000,H=220,L=50,R=18,T=14,B=28;let min=Math.min(0,...values),max=Math.max(0,...values);if(max===min){max+=1;min-=1}const span=max-min;
 const x=i=>L+(W-L-R)*(pts.length===1?1:i/(pts.length-1));const y=v=>T+(H-T-B)*(1-(v-min)/span);const line=pts.map((p,i)=>x(i).toFixed(1)+','+y(Number(p.pnl)).toFixed(1)).join(' ');const zero=y(0).toFixed(1);const area=L+','+zero+' '+line+' '+x(pts.length-1).toFixed(1)+','+zero;const cls=last>0?'green':last<0?'red':'';
 box.innerHTML=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="${cls}"><line class="pnl-zero" x1="${L}" y1="${zero}" x2="${W-R}" y2="${zero}"></line><polygon class="pnl-fill" points="${area}"></polygon><polyline class="pnl-line" points="${line}"></polyline><text class="pnl-axis" x="4" y="${Math.max(18,y(max)+8)}">${esc(money(max))}</text><text class="pnl-axis" x="4" y="${Math.min(H-4,y(min)+8)}">${esc(money(min))}</text></svg>`;
}
'''
    html = html.replace(
        "function setPnl(el,v){const n=Number(v||0);el.textContent=money(n);el.className='value '+(n>0?'green':n<0?'red':'')}",
        "function setPnl(el,v){const n=Number(v||0);el.textContent=money(n);el.className='value '+(n>0?'green':n<0?'red':'')}" + chart_js,
    )

    old_lr = "const lr=d.live_trades.map(x=>`<tr><td><span class=\"status\">LIVE</span></td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.entry_price)}</td><td>${price(x.current_midpoint)}</td><td>${money(x.budget_usdc)}</td><td class=\"${Number(x.estimated_pnl)>0?'green':Number(x.estimated_pnl)<0?'red':''}\">${money(x.estimated_pnl)}</td><td>${when(x.submitted_at)}</td></tr>`);"
    new_lr = "const lr=d.live_trades.map(x=>`<tr><td><span class=\"status\">${x.paper?'PAPER':'LIVE'}</span></td><td class=\"${String(x.side||'BUY').toUpperCase()==='SELL'?'side-sell':'side-buy'}\">${esc(x.side||'BUY')}</td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.entry_price)}</td><td>${price(x.current_price||x.current_sell_price||x.current_midpoint)}</td><td>${money(x.budget_usdc)}</td><td class=\"${Number(x.estimated_pnl)>0?'green':Number(x.estimated_pnl)<0?'red':''}\">${money(x.estimated_pnl)}<span class=\"pnl-sub\">live</span></td><td>${when(x.submitted_at)}</td></tr>`);"
    html = html.replace(old_lr, new_lr)

    old_current = "table(['Status','Market','Outcome','Entry','Mid','Budget','Est. P/L','Submitted'],lr);"
    new_current = "table(['Status','Side','Market','Outcome','Entry','Live price','Budget','Live P/L','Submitted'],lr);renderPnlChart(d.pnl_history||[]);"
    html = html.replace(old_current, new_current)

    old_hr = "const hr=d.history.map(x=>`<tr><td><span class=\"status\">${esc(x.status||'—')}</span></td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.limit_price)}</td><td>${money(x.budget_usdc)}</td><td>${x.auto?'Auto':'Manual'}</td><td>${when(x.submitted_at)}</td><td class=\"muted\">${esc(x.order_id||'—')}</td></tr>`);"
    new_hr = "const hr=d.history.map(x=>`<tr><td><span class=\"status\">${esc(x.status||'—')}</span></td><td class=\"${String(x.side||'BUY').toUpperCase()==='SELL'?'side-sell':'side-buy'}\">${esc(x.side||'BUY')}</td><td class=\"market\">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.trade_price||x.limit_price)}</td><td class=\"${Number(x.pnl)>0?'green':Number(x.pnl)<0?'red':''}\">${x.pnl===null||x.pnl===undefined?'—':money(x.pnl)}${x.pnl_type?`<span class=\"pnl-sub\">${esc(x.pnl_type)}</span>`:''}</td><td>${money(x.budget_usdc)}</td><td>${x.auto?'Auto':'Manual'}</td><td>${when(x.submitted_at)}</td><td class=\"muted\">${esc(x.order_id||'—')}</td></tr>`);"
    html = html.replace(old_hr, new_hr)

    html = html.replace(
        "table(['Status','Market','Outcome','Limit','Budget','Source','Time','Order ID'],hr);",
        "table(['Status','Side','Market','Outcome','Price','P/L','Budget','Source','Time','Order ID'],hr);",
    )

    # The Slack debug layer already changed the polling footer. Make the trading dashboard live at 5s.
    html = html.replace(
        'load();loadSlackDebug();setInterval(load,15000);setInterval(loadSlackDebug,5000);',
        'load();loadSlackDebug();setInterval(load,5000);setInterval(loadSlackDebug,5000);',
    )
    dashboard.DASHBOARD_HTML = html


_install_trading_ui()
