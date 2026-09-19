from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from app import dashboard_metrics_v3 as base

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.ingest
remote = base.remote

SLACK_MODE_FILE = core.DATA_DIR / "slack_trading_mode.json"


class SlackTradingMode(BaseModel):
    live_enabled: bool
    stake_usdc: Decimal = Field(gt=0, le=100)


def _mode() -> dict[str, Any]:
    saved = core._load(SLACK_MODE_FILE)
    # Railway AUTO_TRADING is authoritative. When enabled, no saved/dashboard\n    # state may disable automatic live preparation/execution.\n    auto_prepare_enabled = bool(core.AUTO_TRADING) or bool(saved.get("auto_prepare_enabled", saved.get("live_enabled", False)))
    default_stake = min(Decimal("5"), core.MAX_AUTO_TRADE_USDC)
    stake = Decimal(str(saved.get("stake_usdc") or default_stake))
    stake = min(stake, core.MAX_AUTO_TRADE_USDC)
    return {"auto_prepare_enabled": auto_prepare_enabled, "live_enabled": bool(core.AUTO_TRADING), "stake_usdc": str(stake), "railway_override": bool(core.AUTO_TRADING)}


def _save_mode(auto_prepare_enabled: bool, stake_usdc: Decimal) -> dict[str, Any]:
    stake = min(Decimal(str(stake_usdc)), core.MAX_AUTO_TRADE_USDC)
    effective_auto = bool(core.AUTO_TRADING) or bool(auto_prepare_enabled)
    data = {"auto_prepare_enabled": effective_auto, "live_enabled": bool(core.AUTO_TRADING), "stake_usdc": str(stake), "railway_override": bool(core.AUTO_TRADING)}
    core._save(SLACK_MODE_FILE, data)
    # Slack can prepare live orders, but never dispatch them unattended.
    ingest.SLACK_PAPER_ONLY = not effective_auto
    return data


def _executor_ready() -> tuple[bool, dict[str, Any]]:
    state = remote._state()
    last_seen = float(state.get("last_seen_unix") or 0)
    connected = bool(
        state.get("token_hash")
        and last_seen
        and time.time() - last_seen <= remote.CONNECTED_SECONDS
    )
    ready = bool(
        remote.REMOTE_EXECUTION_ENABLED
        and connected
        and not state.get("geo_blocked")
    )
    return ready, state


def _active_or_pending(asset_id: str, market_url: str, outcome: str) -> bool:
    executions = core._load(core.EXECUTIONS_FILE)
    for rec in executions.values():
        if rec.get("paper") or rec.get("source") != "slack_live":
            continue
        if rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
            continue
        q = rec.get("quote") or {}
        if str(q.get("asset_id") or "") == asset_id:
            return True

    queue = remote._queue_load()
    for rec in queue.values():
        if rec.get("action") != "BUY" or rec.get("status") not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
            continue
        payload = rec.get("payload") or {}
        if payload.get("source") != "slack_live":
            continue
        if (
            str(payload.get("market_url") or "") == market_url
            and str(payload.get("outcome") or "").casefold() == outcome.casefold()
        ):
            return True
    return False


_PAPER_HANDLER = ingest._paper_trade_from_alert


def _prepare_remote_buy(payload: dict[str, Any]) -> dict[str, Any]:
    req_id = f"exec-{uuid.uuid4().hex[:14]}"
    record = {
        "id": req_id,
        "action": "BUY",
        "status": "PENDING" if core.AUTO_TRADING else "WAITING_APPROVAL",
        "payload": payload,
        "created_at": ingest._now_iso(),
        "created_unix": time.time(),
        "updated_at": ingest._now_iso(),
    }
    with remote._QUEUE_LOCK:
        data = remote._queue_load()
        data[req_id] = record
        remote._queue_save(data)
    return record


def _slack_trade_handler(
    parsed: dict[str, Any],
    slack_event_id: str,
    slack_event: dict[str, Any],
) -> dict[str, Any]:
    mode = _mode()
    ingest.SLACK_PAPER_ONLY = not mode["auto_prepare_enabled"]
    if not mode["auto_prepare_enabled"]:
        # Keep PAPER and LIVE sizing identical: the dashboard stake is the
        # single source of truth for every new Slack trade.
        ingest.SLACK_PAPER_BUDGET_USDC = Decimal(str(mode["stake_usdc"]))
        return _PAPER_HANDLER(parsed, slack_event_id, slack_event)

    if parsed.get("market_kind") != "moneyline":
        raise ValueError("Slack LIVE mode is hard-locked to moneyline only")
    if not parsed.get("selection"):
        raise ValueError("Could not identify the predicted winner")

    event, market, outcome_label, outcome_obj = ingest._find_market(parsed)
    asset_id = str(
        getattr(outcome_obj, "token_id", None)
        or getattr(outcome_obj, "position_id", None)
        or ""
    )
    if not asset_id:
        raise ValueError("Matched moneyline has no tradable asset id")

    with ingest.PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))

    if buy_price <= 0 or buy_price >= 1:
        raise ValueError(f"Invalid current BUY price {buy_price}")
    if buy_price > core.MAX_PRICE:
        raise ValueError(f"Current BUY price {buy_price} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise ValueError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug:
        raise ValueError("Matched Polymarket event has no slug")
    market_url = f"https://polymarket.com/sports/wnba/{event_slug}"

    if _active_or_pending(asset_id, market_url, outcome_label):
        raise ValueError("An open or pending LIVE position already exists for this selection")

    stake = min(Decimal(str(mode["stake_usdc"])), core.MAX_AUTO_TRADE_USDC)
    trade_id = f"slack-live-{slack_event_id[:12]}-{uuid.uuid4().hex[:6]}"
    payload = {
        "market_url": market_url,
        "outcome": outcome_label,
        "market_type": "moneyline",
        "max_price": str(buy_price),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "slack_live",
        "auto": True,
        "slack_event_id": slack_event_id,
    }
    queued = _prepare_remote_buy(payload)
    return {
        "id": trade_id,
        "trade_id": trade_id,
        "paper": False,
        "queued": False,
        "prepared": True,
        "requires_approval": not bool(core.AUTO_TRADING),
        "request_id": queued["id"],
        "source": "slack_live",
        "market": str(getattr(market, "question", None) or getattr(event, "title", "WNBA moneyline")),
        "market_url": market_url,
        "outcome": outcome_label,
        "asset_id": asset_id,
        "buy_price": str(buy_price),
        "spread": str(spread),
        "stake_usdc": str(stake),
        "slack_event_id": slack_event_id,
    }


ingest._paper_trade_from_alert = _slack_trade_handler
ingest.SLACK_PAPER_ONLY = not _mode()["auto_prepare_enabled"]


@app.get("/api/slack/trading-mode", dependencies=[Depends(dashboard._auth)])
def slack_trading_mode_get():
    mode = _mode()
    ready, state = _executor_ready()
    last_seen = float(state.get("last_seen_unix") or 0)
    connected = bool(
        state.get("token_hash")
        and last_seen
        and time.time() - last_seen <= remote.CONNECTED_SECONDS
    )
    return {
        **mode,
        "paper_only": not mode["auto_prepare_enabled"],
        "executor_ready": ready,
        "executor_connected": connected,
        "executor_geo_blocked": state.get("geo_blocked"),
        "executor_country": state.get("geo_country"),
        "executor_region": state.get("geo_region"),
        "max_stake_usdc": str(core.MAX_AUTO_TRADE_USDC),
        "moneyline_only": True,
        "duplicate_position_guard": True,
        "unattended_live_execution": False,
        "approval_required": True,
    }


@app.get("/api/slack/pending-live", dependencies=[Depends(dashboard._auth)])
def slack_pending_live():
    queue = remote._queue_load()
    rows = []
    for rec in queue.values():
        payload = rec.get("payload") or {}
        if rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL" or payload.get("source") != "slack_live":
            continue
        rows.append({
            "request_id": rec.get("id"),
            "created_at": rec.get("created_at"),
            "market_url": payload.get("market_url"),
            "outcome": payload.get("outcome"),
            "max_price": payload.get("max_price"),
            "budget_usdc": payload.get("budget_usdc"),
            "slack_event_id": payload.get("slack_event_id"),
        })
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"pending": rows[:50]}


@app.post("/api/slack/approve-live/{request_id}", dependencies=[Depends(dashboard._auth)])
def slack_approve_live(request_id: str):
    ready, state = _executor_ready()
    if not ready:
        if state.get("geo_blocked"):
            raise HTTPException(status_code=409, detail="Termux executor is geoblocked")
        raise HTTPException(status_code=409, detail="Termux executor is not connected")
    with remote._QUEUE_LOCK:
        queue = remote._queue_load()
        rec = queue.get(request_id)
        if not rec or rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
            raise HTTPException(status_code=404, detail="Prepared Slack order is not awaiting approval")
        rec["status"] = "PENDING"
        rec["approved_at"] = ingest._now_iso()
        rec["updated_at"] = ingest._now_iso()
        queue[request_id] = rec
        remote._queue_save(queue)
    return {"ok": True, "request_id": request_id, "status": "PENDING"}


@app.post("/api/slack/reject-live/{request_id}", dependencies=[Depends(dashboard._auth)])
def slack_reject_live(request_id: str):
    with remote._QUEUE_LOCK:
        queue = remote._queue_load()
        rec = queue.get(request_id)
        if not rec or rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
            raise HTTPException(status_code=404, detail="Prepared Slack order is not awaiting approval")
        rec["status"] = "CANCELLED"
        rec["rejected_at"] = ingest._now_iso()
        rec["updated_at"] = ingest._now_iso()
        queue[request_id] = rec
        remote._queue_save(queue)
    return {"ok": True, "request_id": request_id, "status": "CANCELLED"}


@app.put("/api/slack/trading-mode", dependencies=[Depends(dashboard._auth)])
def slack_trading_mode_put(req: SlackTradingMode):
    if req.stake_usdc > core.MAX_AUTO_TRADE_USDC:
        raise HTTPException(
            status_code=400,
            detail=f"Slack stake cannot exceed the dashboard Auto trade cap of {core.MAX_AUTO_TRADE_USDC} USDC",
        )
    saved = _save_mode(req.live_enabled, req.stake_usdc)
    return {
        "ok": True,
        **saved,
        "paper_only": not saved["auto_prepare_enabled"],
        "unattended_live_execution": False,
        "approval_required": True,
    }


def _install_slack_live_controls() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="slackTradingMode"' in html:
        return

    box = """
    <div class="slack-mode-box">
      <div class="slack-mode-head">
        <div><div class="label">Slack WNBA live auto-prepare</div><div class="slack-mode-value" id="slackTradingMode">Checking…</div></div>
        <div class="slack-mode-state" id="slackExecutorState">Executor status…</div>
      </div>
      <div class="slack-mode-controls">
        <label>Stake per Slack alert (USDC, max <span id="slackStakeMax">—</span>)</label>
        <input id="slackLiveStake" type="number" min="0.01" step="0.01" value="5.00">
        <button type="button" class="mode-paper-btn" id="slackPaperBtn">PAPER</button>
        <button type="button" class="mode-live-btn" id="slackLiveBtn">ENABLE AUTO-PREPARE</button>
      </div>
      <div class="slack-mode-note">AUTO-PREPARE builds qualifying Predicted Winner moneyline orders from Slack alerts and places them in the approval queue. The Configuration → Auto trade cap is the maximum stake. No real order is sent to the Termux executor until you approve that specific order.</div><div id="slackPendingApprovals" class="slack-pending"></div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', box + '<form id="settingsForm">', 1)

    css = """
.slack-mode-box{border:1px solid var(--border);border-radius:12px;background:#0d1522;padding:14px;margin:0 0 16px}.slack-pending{margin-top:12px}.slack-pending-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-top:1px solid var(--border);padding:9px 0}.slack-pending-actions{display:flex;gap:6px}.slack-approve-btn,.slack-reject-btn{border-radius:8px;padding:6px 9px;font-size:11px;font-weight:850;cursor:pointer}.slack-approve-btn{border:1px solid #2e7d64;background:#12362d;color:#a7f3d0}.slack-reject-btn{border:1px solid #7f1d1d;background:#3f1118;color:#fecdd3}.slack-mode-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.slack-mode-value{font-size:19px;font-weight:900;margin-top:5px}.slack-mode-state{font-size:11px;color:var(--muted);text-align:right}.slack-mode-controls{display:flex;gap:9px;align-items:end;flex-wrap:wrap;margin-top:12px}.slack-mode-controls label{width:100%;font-size:10px;color:var(--muted);text-transform:uppercase;font-weight:800;letter-spacing:.07em}.slack-mode-controls input{width:145px;background:#080e18;border:1px solid #344157;color:#fff;border-radius:9px;padding:9px;font:inherit}.mode-paper-btn,.mode-live-btn{border-radius:9px;padding:9px 13px;font-weight:850;cursor:pointer}.mode-paper-btn{border:1px solid #3d4d67;background:#172033;color:#fff}.mode-live-btn{border:1px solid #7f1d1d;background:#3f1118;color:#fecdd3}.mode-live-btn.active{background:#7f1d1d;color:#fff}.mode-paper-btn.active{border-color:#2e7d64;background:#12362d;color:#a7f3d0}.slack-mode-note{font-size:10px;color:var(--muted);margin-top:9px;line-height:1.45}@media(max-width:650px){.slack-mode-head{align-items:flex-start;flex-direction:column}.slack-mode-state{text-align:left}}
"""
    html = html.replace("</style>", css + "</style>", 1)

    js = r"""
async function loadSlackTradingMode(){
 try{
  const r=await fetch('/api/slack/trading-mode',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Could not load Slack mode');
  const live=!!d.auto_prepare_enabled;
  const mode=document.getElementById('slackTradingMode'),state=document.getElementById('slackExecutorState'),stake=document.getElementById('slackLiveStake');
  mode.textContent=d.railway_override?'LIVE AUTO · RAILWAY OVERRIDE':(live?'LIVE AUTO-PREPARE · APPROVAL REQUIRED':'PAPER ONLY');
  mode.className='slack-mode-value '+(live?'red':'green');
  state.textContent=(d.executor_connected?'Termux connected':'Termux offline')+(d.executor_geo_blocked?' · BLOCKED':'')+(d.executor_country?' · '+d.executor_country+'/'+(d.executor_region||''):'');
  try{const pr=await fetch('/api/slack/pending-live',{cache:'no-store'}),pd=await pr.json();const box=document.getElementById('slackPendingApprovals');const rows=(pd.pending||[]);box.innerHTML=rows.length?'<div class="label" style="margin-bottom:5px">Awaiting approval</div>'+rows.map(x=>`<div class="slack-pending-row"><div><b>${x.outcome||'Order'}</b><div class="muted">${Number(x.budget_usdc||0).toFixed(2)} · max ${Number(x.max_price||0).toFixed(3)}</div></div><div class="slack-pending-actions"><button class="slack-approve-btn" data-slack-approve="${x.request_id}">APPROVE</button><button class="slack-reject-btn" data-slack-reject="${x.request_id}">REJECT</button></div></div>`).join(''):''}catch(_e){}
  if(stake&&!stake.dataset.dirty)stake.value=d.stake_usdc;
  document.getElementById('slackStakeMax').textContent='$'+Number(d.max_stake_usdc).toFixed(2);
  document.getElementById('slackPaperBtn').classList.toggle('active',!live);
  document.getElementById('slackLiveBtn').classList.toggle('active',live);
 }catch(e){
  const m=document.getElementById('slackTradingMode');
  if(m)m.textContent='Mode error: '+String(e);
 }
}
async function setSlackTradingMode(live){
 const stake=document.getElementById('slackLiveStake').value;
 if(live&&!confirm('Enable LIVE AUTO-PREPARE?'))return;
 try{
  const r=await fetch('/api/slack/trading-mode',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({live_enabled:live,stake_usdc:stake})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Mode change failed');
  delete document.getElementById('slackLiveStake').dataset.dirty;
  await loadSlackTradingMode();
 }catch(e){alert('Slack trading mode change failed: '+String(e))}
}
document.getElementById('slackLiveStake').addEventListener('input',function(e){e.target.dataset.dirty='1'});
document.getElementById('slackPaperBtn').addEventListener('click',function(){setSlackTradingMode(false)});
document.getElementById('slackLiveBtn').addEventListener('click',function(){setSlackTradingMode(true)});
document.addEventListener('click',async function(ev){const a=ev.target.closest('[data-slack-approve]'),r=ev.target.closest('[data-slack-reject]');if(!a&&!r)return;const id=(a||r).getAttribute(a?'data-slack-approve':'data-slack-reject');if(a&&!confirm('Approve this prepared live order for execution?'))return;try{const resp=await fetch(a?'/api/slack/approve-live/'+encodeURIComponent(id):'/api/slack/reject-live/'+encodeURIComponent(id),{method:'POST'}),d=await resp.json();if(!resp.ok)throw new Error(d.detail||'Action failed');await loadSlackTradingMode()}catch(e){alert(String(e))}});
loadSlackTradingMode();
setInterval(loadSlackTradingMode,5000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_slack_live_controls()
