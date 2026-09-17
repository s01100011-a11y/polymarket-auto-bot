from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException

from app import termux_executor_dashboard_v2 as base

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.ingest
remote = base.base

_RECONCILE_LOCK = threading.Lock()
_RECONCILE_LAST = 0.0
_RECONCILE_MIN_SECONDS = 5.0
_EPS = Decimal("0.0001")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _wallet_for_reconciliation() -> str | None:
    try:
        state = remote._state()
        wallet = str(state.get("wallet") or "").strip()
        return wallet or None
    except Exception:
        return None


def _full_wallet_positions(wallet: str) -> dict[str, Any]:
    positions: dict[str, Any] = {}
    with ingest.PublicClient() as client:
        paginator = client.list_positions(user=wallet, full_history=True, page_size=100)
        for pos in paginator.iter_items():
            asset_id = str(getattr(pos, "asset_id", None) or getattr(pos, "token_id", None) or "")
            if asset_id:
                positions[asset_id] = pos
    return positions


def _executor_confirmed_closed_trade_ids() -> set[str]:
    """Return trades for which Termux already confirmed the tracked wallet shares are zero."""
    marker = "Wallet no longer holds the tracked test shares"
    try:
        queue = remote._queue_load()
    except Exception:
        return set()
    closed: set[str] = set()
    for item in queue.values():
        if item.get("action") != "SELL" or item.get("status") != "FAILED":
            continue
        if marker not in str(item.get("error") or ""):
            continue
        trade_id = str((item.get("payload") or {}).get("trade_id") or "")
        if trade_id:
            closed.add(trade_id)
    return closed


def _reconcile_live_positions(force: bool = False) -> dict[str, Any]:
    global _RECONCILE_LAST
    now = time.time()
    if not force and now - _RECONCILE_LAST < _RECONCILE_MIN_SECONDS:
        return {"ok": True, "skipped": "recent"}

    with _RECONCILE_LOCK:
        now = time.time()
        if not force and now - _RECONCILE_LAST < _RECONCILE_MIN_SECONDS:
            return {"ok": True, "skipped": "recent"}

        wallet = _wallet_for_reconciliation()
        if not wallet:
            return {"ok": False, "reason": "Termux wallet is not known yet"}

        executions = core._load(core.EXECUTIONS_FILE)
        active = [
            rec
            for rec in executions.values()
            if rec.get("source") == "termux_executor"
            and not rec.get("paper")
            and rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}
        ]
        if not active:
            _RECONCILE_LAST = now
            return {"ok": True, "checked": 0, "changed": 0}

        changed = 0
        checked = 0
        stamp = _now_iso()
        executor_confirmed_closed = _executor_confirmed_closed_trade_ids()
        unresolved: list[dict[str, Any]] = []
        for rec in active:
            if str(rec.get("id") or "") not in executor_confirmed_closed:
                unresolved.append(rec)
                continue
            rec["status"] = "CLOSED_RECONCILED"
            rec["remaining_shares"] = "0"
            rec["closed_at"] = rec.get("closed_at") or stamp
            rec["wallet_reconciled_at"] = stamp
            rec["wallet_position_size"] = "0"
            rec["reconciliation_note"] = (
                "Termux checked the Polymarket wallet before SELL and found zero tracked shares. "
                "The position was already closed outside this SELL request; exit price and realized P/L remain unknown."
            )
            changed += 1

        if changed:
            core._save(core.EXECUTIONS_FILE, executions)
        if not unresolved:
            _RECONCILE_LAST = now
            return {"ok": True, "checked": len(active), "changed": changed}

        try:
            positions = _full_wallet_positions(wallet)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "changed": changed}

        for rec in unresolved:
            q = rec.get("quote") or {}
            asset_id = str(q.get("asset_id") or "")
            if not asset_id or asset_id not in positions:
                continue

            checked += 1
            pos = positions[asset_id]
            wallet_size = _d(getattr(pos, "current_size", "0"))
            pos_status = str(getattr(pos, "status", "") or "")
            pre_size = _d(rec.get("pre_position_size"))
            filled = _d(rec.get("filled_shares") or q.get("shares"))
            old_remaining = _d(
                rec.get("remaining_shares")
                if rec.get("remaining_shares") is not None
                else filled
            )

            attributable = max(Decimal("0"), wallet_size - pre_size)
            if filled > 0:
                attributable = min(attributable, filled)

            rec["wallet_reconciled_at"] = stamp
            rec["wallet_position_size"] = str(wallet_size)
            rec["wallet_position_status"] = pos_status

            if attributable <= _EPS:
                rec["status"] = "CLOSED_RECONCILED"
                rec["remaining_shares"] = "0"
                rec["closed_at"] = rec.get("closed_at") or stamp
                rec["reconciliation_note"] = (
                    "Polymarket wallet position is closed. Realized P/L is left unknown "
                    "unless this bot recorded the exit execution."
                )
                changed += 1
            elif old_remaining > 0 and attributable + _EPS < old_remaining:
                rec["status"] = "PARTIALLY_CLOSED"
                rec["remaining_shares"] = str(attributable)
                rec["reconciliation_note"] = "Remaining shares reconciled from Polymarket wallet holdings."
                changed += 1

        if changed:
            core._save(core.EXECUTIONS_FILE, executions)
        _RECONCILE_LAST = now
        return {"ok": True, "checked": checked, "changed": changed}


@app.post("/api/dashboard/reconcile", dependencies=[Depends(dashboard._auth)])
def reconcile_now():
    return _reconcile_live_positions(force=True)


@app.post("/api/paper/close/{trade_id}", dependencies=[Depends(dashboard._auth)])
def close_paper_trade(trade_id: str):
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown paper trade")
    if not rec.get("paper"):
        raise HTTPException(status_code=400, detail="This endpoint only closes paper trades")
    if rec.get("status") != "PAPER_OPEN":
        raise HTTPException(status_code=400, detail=f"Paper trade status is {rec.get('status')}")

    q = rec.get("quote") or {}
    asset_id = str(q.get("asset_id") or "")
    shares = _d(
        rec.get("remaining_shares")
        if rec.get("remaining_shares") is not None
        else q.get("shares")
    )
    entry = _d(q.get("paper_entry_price") or q.get("entry_price") or q.get("limit_price"))
    if not asset_id or shares <= 0 or entry <= 0:
        raise HTTPException(status_code=400, detail="Paper trade is missing asset, shares, or entry price")

    try:
        with ingest.PublicClient() as client:
            exit_price = _d(client.get_price(asset_id=asset_id, side="SELL"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not get current SELL price: {exc}") from exc

    if exit_price <= 0 or exit_price >= 1:
        raise HTTPException(status_code=502, detail=f"Invalid current SELL price {exit_price}")

    realized = ((exit_price - entry) * shares).quantize(Decimal("0.01"))
    proceeds = (exit_price * shares).quantize(Decimal("0.01"))
    stamp = _now_iso()

    rec["status"] = "PAPER_CLOSED"
    rec["exit_price"] = str(exit_price)
    rec["realized_pnl"] = str(realized)
    rec["remaining_shares"] = "0"
    rec["closed_at"] = stamp
    rec.setdefault("execution", {})["paper_close"] = {
        "at": stamp,
        "exit_price": str(exit_price),
        "shares": str(shares),
        "proceeds_usdc": str(proceeds),
    }
    executions[trade_id] = rec

    sell_id = f"{trade_id}-paper-sell-{uuid.uuid4().hex[:6]}"
    executions[sell_id] = {
        "id": sell_id,
        "status": "PAPER_SELL",
        "created_at": stamp,
        "submitted_at": stamp,
        "side": "SELL",
        "auto": False,
        "paper": True,
        "source": rec.get("source") or "paper",
        "parent_trade_id": trade_id,
        "display_pnl": str(realized),
        "budget_usdc": str(proceeds),
        "quote": {
            "market": q.get("market"),
            "market_url": q.get("market_url"),
            "market_type": q.get("market_type"),
            "requested_outcome": q.get("requested_outcome"),
            "resolved_outcome": q.get("resolved_outcome"),
            "asset_id": asset_id,
            "limit_price": str(exit_price),
            "shares": str(shares),
        },
        "execution": {
            "placed": False,
            "paper": True,
            "reason": "Paper position closed at the current Polymarket SELL price; no real order was sent.",
        },
    }
    core._save(core.EXECUTIONS_FILE, executions)

    return {
        "ok": True,
        "trade_id": trade_id,
        "status": "PAPER_CLOSED",
        "exit_price": str(exit_price),
        "shares": str(shares),
        "realized_pnl": str(realized),
        "proceeds_usdc": str(proceeds),
    }


_BASE_SNAPSHOT = dashboard._dashboard_snapshot


def _performance_metrics(executions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    wins = 0
    losses = 0
    pushes = 0
    realized_total = Decimal("0")
    stake_total = Decimal("0")
    graded = 0

    for rec in executions.values():
        if rec.get("parent_trade_id"):
            continue
        if rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
            continue

        realized = base.pnl_base._explicit_realized_pnl(rec)
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
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "graded_trades": graded,
        "graded_stake_usdc": str(stake_total.quantize(Decimal("0.01"))),
        "realized_pnl_for_metrics": str(realized_total.quantize(Decimal("0.01"))),
        "accuracy_pct": str(accuracy.quantize(Decimal("0.1"))) if accuracy is not None else None,
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
    }


def _dashboard_snapshot_v3() -> dict[str, Any]:
    reconcile = _reconcile_live_positions()
    data = _BASE_SNAPSHOT()
    executions = core._load(core.EXECUTIONS_FILE)
    metrics = _performance_metrics(executions)
    status = data.setdefault("status", {})
    status.update(metrics)
    status["reconciliation"] = reconcile
    status["submitted_live_trades"] = len(data.get("live_trades", []))
    status["performance_note"] = (
        "ROI and accuracy use closed trades with known realized P/L. "
        "Accuracy excludes pushes; externally reconciled closes without a recorded exit are not graded."
    )
    return data


dashboard._dashboard_snapshot = _dashboard_snapshot_v3


def _install_performance_and_paper_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="performanceRoi"' in html:
        return

    performance_html = '''
  <div class="performance-strip">
    <div class="performance-card"><div class="label">ROI · closed trades</div><div class="performance-value" id="performanceRoi">—</div></div>
    <div class="performance-card"><div class="label">Win / Loss</div><div class="performance-value" id="performanceWL">—</div><div class="performance-sub" id="performancePushes"></div></div>
    <div class="performance-card"><div class="label">Accuracy</div><div class="performance-value" id="performanceAccuracy">—</div><div class="performance-sub" id="performanceGraded"></div></div>
  </div>
'''
    html = html.replace('  <div class="tabs">', performance_html + '  <div class="tabs">', 1)

    css = '''
.performance-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}.performance-card{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:14px;padding:15px}.performance-value{font-size:24px;font-weight:900;margin-top:6px}.performance-sub{font-size:10px;color:var(--muted);margin-top:4px}.paper-close-btn{border:1px solid #7c5b15;background:#33270c;color:#fde68a;border-radius:8px;padding:7px 11px;font-size:11px;font-weight:850;cursor:pointer;white-space:nowrap}.paper-close-btn:hover{background:#49350d}.paper-close-btn:disabled{opacity:.55;cursor:wait}@media(max-width:700px){.performance-strip{grid-template-columns:1fr 1fr}.performance-card:last-child{grid-column:1/-1}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    live_only = """${(!x.paper&&x.source==='termux_executor')?`<button class="live-sell-btn" data-live-sell="${esc(x.id||'')}">SELL</button>`:'—'}"""
    with_paper = """${x.paper?`<button class="paper-close-btn" data-paper-close="${esc(x.id||'')}">CLOSE</button>`:((x.source==='termux_executor')?`<button class="live-sell-btn" data-live-sell="${esc(x.id||'')}">SELL</button>`:'—')}"""
    html = html.replace(live_only, with_paper)
    metric_anchor = "document.getElementById('budget').textContent=money(s.daily_budget_used)+' / '+money(s.max_daily_budget_usdc);"
    metric_js = """document.getElementById('budget').textContent=money(s.daily_budget_used)+' / '+money(s.max_daily_budget_usdc);
  const roi=document.getElementById('performanceRoi'),wl=document.getElementById('performanceWL'),acc=document.getElementById('performanceAccuracy'),push=document.getElementById('performancePushes'),graded=document.getElementById('performanceGraded');
  if(roi){const n=Number(s.roi_pct);roi.textContent=s.roi_pct===null||s.roi_pct===undefined?'—':n.toFixed(1)+'%';roi.className='performance-value '+(n>0?'green':n<0?'red':'')}
  if(wl)wl.textContent=String(s.wins||0)+' / '+String(s.losses||0);
  if(push)push.textContent=(s.pushes||0)+' push'+((s.pushes||0)===1?'':'es');
  if(acc){const a=Number(s.accuracy_pct);acc.textContent=s.accuracy_pct===null||s.accuracy_pct===undefined?'—':a.toFixed(1)+'%'}
  if(graded)graded.textContent=(s.graded_trades||0)+' graded closed trades';"""
    html = html.replace(metric_anchor, metric_js)

    paper_js = r'''
document.addEventListener('click',async function(ev){
 const btn=ev.target.closest('[data-paper-close]');
 if(!btn)return;
 const tradeId=btn.getAttribute('data-paper-close');
 if(!tradeId)return;
 if(!confirm('Close this PAPER position at the current Polymarket SELL price? No real order will be sent.'))return;
 const original=btn.textContent;
 try{
  btn.disabled=true;btn.textContent='CLOSING…';
  const r=await fetch('/api/paper/close/'+encodeURIComponent(tradeId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Paper close failed');
  btn.textContent='CLOSED';
  setTimeout(()=>load(),350);
 }catch(e){
  btn.disabled=false;btn.textContent=original;
  alert('Paper close failed: '+String(e));
 }
});
'''
    html = html.replace("</script>", paper_js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_performance_and_paper_ui()
