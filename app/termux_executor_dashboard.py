from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app import live_test_dashboard as base
from app import live_trading

app = base.app
dashboard = base.dashboard
core = base.core
ingest = live_trading.ingest

REMOTE_EXECUTION_ENABLED = os.getenv("REMOTE_EXECUTION_ENABLED", "true").lower() == "true"
REMOTE_MAX_USDC = Decimal(os.getenv("REMOTE_MAX_USDC", "5"))
EXECUTOR_QUEUE_FILE = core.DATA_DIR / "termux_executor_queue.json"
EXECUTOR_STATE_FILE = core.DATA_DIR / "termux_executor_state.json"
_QUEUE_LOCK = threading.Lock()
PAIR_TTL_SECONDS = 1800
LEASE_SECONDS = 20
CONNECTED_SECONDS = 15
MAX_QUEUE_ITEMS = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state() -> dict[str, Any]:
    return core._load(EXECUTOR_STATE_FILE)


def _save_state(state: dict[str, Any]) -> None:
    core._save(EXECUTOR_STATE_FILE, state)


def _ensure_pair_code() -> tuple[str | None, dict[str, Any]]:
    state = _state()
    if state.get("token_hash"):
        return None, state
    now = time.time()
    if not state.get("pair_code") or float(state.get("pair_expires_unix") or 0) <= now:
        state["pair_code"] = f"{secrets.randbelow(1_000_000):06d}"
        state["pair_expires_unix"] = now + PAIR_TTL_SECONDS
        state["pair_created_at"] = _now_iso()
        _save_state(state)
    return str(state["pair_code"]), state


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    name: str = Field(default="termux", min_length=1, max_length=80)


class ExecutorResult(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)


class Heartbeat(BaseModel):
    name: str = Field(default="termux", min_length=1, max_length=80)
    wallet: str | None = Field(default=None, max_length=120)
    wallet_type: str | None = Field(default=None, max_length=80)
    usdc_balance: str | None = Field(default=None, max_length=80)
    portfolio_value: str | None = Field(default=None, max_length=80)
    geo_country: str | None = Field(default=None, max_length=20)
    geo_region: str | None = Field(default=None, max_length=40)
    geo_blocked: bool | None = None
    status: str | None = Field(default=None, max_length=300)


def _executor_auth(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Executor bearer token required")
    token = authorization[7:].strip()
    state = _state()
    expected = str(state.get("token_hash") or "")
    if not token or not expected or not secrets.compare_digest(_token_hash(token), expected):
        raise HTTPException(status_code=401, detail="Invalid executor token")
    state["last_seen_unix"] = time.time()
    state["last_seen_at"] = _now_iso()
    _save_state(state)
    return state


@app.post("/api/executor/pair")
def executor_pair(req: PairRequest):
    code, state = _ensure_pair_code()
    if state.get("token_hash"):
        raise HTTPException(status_code=409, detail="An executor is already paired")
    if not code or not secrets.compare_digest(req.code.strip(), code):
        raise HTTPException(status_code=401, detail="Invalid pairing code")
    if float(state.get("pair_expires_unix") or 0) <= time.time():
        raise HTTPException(status_code=401, detail="Pairing code expired")
    token = secrets.token_urlsafe(40)
    state.pop("pair_code", None)
    state.pop("pair_expires_unix", None)
    state["token_hash"] = _token_hash(token)
    state["paired_at"] = _now_iso()
    state["worker_name"] = req.name
    state["last_seen_unix"] = time.time()
    state["last_seen_at"] = _now_iso()
    _save_state(state)
    return {"ok": True, "token": token, "worker_name": req.name}


@app.post("/api/executor/unpair", dependencies=[Depends(dashboard._auth)])
def executor_unpair():
    _save_state({})
    code, _ = _ensure_pair_code()
    return {"ok": True, "pair_code": code}


@app.get("/api/executor/status", dependencies=[Depends(dashboard._auth)])
def executor_status():
    code, state = _ensure_pair_code()
    last_seen = float(state.get("last_seen_unix") or 0)
    paired = bool(state.get("token_hash"))
    return {
        "enabled": REMOTE_EXECUTION_ENABLED,
        "paired": paired,
        "connected": bool(paired and last_seen and time.time() - last_seen <= CONNECTED_SECONDS),
        "pair_code": code,
        "pair_expires_unix": state.get("pair_expires_unix"),
        "worker_name": state.get("worker_name"),
        "last_seen_at": state.get("last_seen_at"),
        "wallet": state.get("wallet"),
        "wallet_type": state.get("wallet_type"),
        "usdc_balance": state.get("usdc_balance"),
        "portfolio_value": state.get("portfolio_value"),
        "geo_country": state.get("geo_country"),
        "geo_region": state.get("geo_region"),
        "geo_blocked": state.get("geo_blocked"),
        "worker_status": state.get("status"),
        "remote_max_usdc": str(REMOTE_MAX_USDC),
        "railway_live_trading": core.LIVE_TRADING,
    }


@app.post("/api/executor/heartbeat")
def executor_heartbeat(req: Heartbeat, _: dict[str, Any] = Depends(_executor_auth)):
    state = _state()
    state.update({
        "worker_name": req.name,
        "last_seen_unix": time.time(),
        "last_seen_at": _now_iso(),
        "wallet": req.wallet,
        "wallet_type": req.wallet_type,
        "usdc_balance": req.usdc_balance,
        "portfolio_value": req.portfolio_value,
        "geo_country": req.geo_country,
        "geo_region": req.geo_region,
        "geo_blocked": req.geo_blocked,
        "status": req.status,
    })
    _save_state(state)
    return {"ok": True}


@app.get("/api/executor/wallet", dependencies=[Depends(dashboard._auth)])
def executor_wallet():
    state = _state()
    last_seen = float(state.get("last_seen_unix") or 0)
    connected = bool(state.get("token_hash") and last_seen and time.time() - last_seen <= CONNECTED_SECONDS)
    return {
        "ok": connected,
        "connected": connected,
        "wallet": state.get("wallet") or "Termux executor not connected",
        "wallet_type": state.get("wallet_type") or "REMOTE_EXECUTOR",
        "usdc_balance": state.get("usdc_balance") or "0",
        "portfolio_value": state.get("portfolio_value"),
        "live_trading": bool(REMOTE_EXECUTION_ENABLED and connected and not state.get("geo_blocked")),
        "auto_trading": False,
        "slack_paper_only": ingest.SLACK_PAPER_ONLY,
    }


def _queue_load() -> dict[str, Any]:
    return core._load(EXECUTOR_QUEUE_FILE)


def _queue_save(data: dict[str, Any]) -> None:
    if len(data) > MAX_QUEUE_ITEMS:
        ordered = sorted(data.items(), key=lambda kv: kv[1].get("created_unix", 0))
        data = dict(ordered[-MAX_QUEUE_ITEMS:])
    core._save(EXECUTOR_QUEUE_FILE, data)


def _enqueue(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not REMOTE_EXECUTION_ENABLED:
        raise HTTPException(status_code=409, detail="Remote execution is disabled")
    req_id = f"exec-{uuid.uuid4().hex[:14]}"
    record = {
        "id": req_id,
        "action": action,
        "status": "PENDING",
        "payload": payload,
        "created_at": _now_iso(),
        "created_unix": time.time(),
        "updated_at": _now_iso(),
    }
    with _QUEUE_LOCK:
        data = _queue_load()
        data[req_id] = record
        _queue_save(data)
    return record


def _validate_remote_buy(req: live_trading.LiveTestBuy) -> None:
    if core._norm(req.market_type) != "moneyline":
        raise HTTPException(status_code=400, detail="Remote BUY is locked to moneyline only")
    if req.budget_usdc > REMOTE_MAX_USDC:
        raise HTTPException(status_code=400, detail=f"Remote test amount exceeds ${REMOTE_MAX_USDC}")
    if req.max_price > core.MAX_PRICE:
        raise HTTPException(status_code=400, detail=f"Maximum price exceeds MAX_PRICE={core.MAX_PRICE}")
    if core._sports_event_slug(str(req.market_url)) is None:
        raise HTTPException(status_code=400, detail="Use a Polymarket /sports/ event URL")


def _buy_payload(req: live_trading.LiveTestBuy, trade_id: str | None = None) -> dict[str, Any]:
    return {
        "market_url": str(req.market_url),
        "outcome": req.outcome,
        "market_type": "moneyline",
        "max_price": str(req.max_price),
        "budget_usdc": str(req.budget_usdc),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
    }


@app.post("/api/executor/request-preview", dependencies=[Depends(dashboard._auth)])
def request_preview(req: live_trading.LiveTestBuy):
    _validate_remote_buy(req)
    rec = _enqueue("PREVIEW", _buy_payload(req))
    return {"ok": True, "queued": True, "request_id": rec["id"]}


@app.post("/api/executor/request-buy", dependencies=[Depends(dashboard._auth)])
def request_buy(req: live_trading.LiveTestBuy):
    _validate_remote_buy(req)
    trade_id = f"live-test-remote-{uuid.uuid4().hex[:10]}"
    rec = _enqueue("BUY", _buy_payload(req, trade_id=trade_id))
    return {"ok": True, "queued": True, "request_id": rec["id"], "trade_id": trade_id}


@app.post("/api/executor/request-sell/{trade_id}", dependencies=[Depends(dashboard._auth)])
def request_sell(trade_id: str):
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown remote test trade")
    if rec.get("source") not in {"termux_executor", "slack_live"} or rec.get("paper"):
        raise HTTPException(status_code=400, detail="Only tracked Termux live positions can be sold here")
    if rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
        raise HTTPException(status_code=400, detail=f"Trade status is {rec.get('status')}")
    q = rec.get("quote") or {}
    shares = str(rec.get("remaining_shares") or rec.get("filled_shares") or q.get("shares") or "0")
    payload = {
        "trade_id": trade_id,
        "asset_id": str(q.get("asset_id") or ""),
        "shares": shares,
        "entry_price": str(q.get("entry_price") or q.get("limit_price") or "0"),
        "market": q.get("market"),
        "market_url": q.get("market_url"),
        "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
    }
    if not payload["asset_id"] or Decimal(shares) <= 0:
        raise HTTPException(status_code=400, detail="Tracked trade has no sellable shares")
    queued = _enqueue("SELL", payload)
    return {"ok": True, "queued": True, "request_id": queued["id"], "trade_id": trade_id}


@app.get("/api/executor/request-status/{request_id}", dependencies=[Depends(dashboard._auth)])
def request_status(request_id: str):
    rec = _queue_load().get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown executor request")
    return {
        "request_id": request_id,
        "action": rec.get("action"),
        "status": rec.get("status"),
        "result": rec.get("result"),
        "error": rec.get("error"),
        "updated_at": rec.get("updated_at"),
    }


@app.get("/api/executor/next")
def executor_next(_: dict[str, Any] = Depends(_executor_auth)):
    now = time.time()
    with _QUEUE_LOCK:
        data = _queue_load()
        candidates = []
        for rec in data.values():
            status = rec.get("status")
            lease_until = float(rec.get("lease_until_unix") or 0)
            if status == "PENDING" or (status == "LEASED" and lease_until <= now):
                candidates.append(rec)
        if not candidates:
            return {"ok": True, "request": None}
        rec = sorted(candidates, key=lambda x: x.get("created_unix", 0))[0]
        rec["status"] = "LEASED"
        rec["lease_until_unix"] = now + LEASE_SECONDS
        rec["updated_at"] = _now_iso()
        data[rec["id"]] = rec
        _queue_save(data)
    return {"ok": True, "request": {"id": rec["id"], "action": rec["action"], "payload": rec.get("payload") or {}}}


def _record_buy_result(queue_rec: dict[str, Any], result: dict[str, Any]) -> None:
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id or not result.get("ok"):
        return
    filled = Decimal(str(result.get("filled_shares") or "0"))
    if filled <= 0:
        return
    now = _now_iso()
    entry_price = str(result.get("entry_price") or payload.get("max_price") or "0")
    estimated_cost = (filled * Decimal(entry_price)).quantize(Decimal("0.01"))
    record = {
        "id": trade_id,
        "status": "ORDER_SUBMITTED",
        "created_at": now,
        "submitted_at": now,
        "side": "BUY",
        "paper": False,
        "source": str(payload.get("source") or "termux_executor"),
        "budget_usdc": str(estimated_cost),
        "slack_event_id": payload.get("slack_event_id"),
        "auto": bool(payload.get("auto", False)),
        "filled_shares": str(filled),
        "pre_position_size": str(result.get("position_before") or "0"),
        "quote": {
            "market": result.get("market"),
            "market_url": payload.get("market_url"),
            "market_type": "moneyline",
            "requested_outcome": payload.get("outcome"),
            "resolved_outcome": result.get("outcome") or payload.get("outcome"),
            "asset_id": str(result.get("asset_id") or ""),
            "limit_price": str(payload.get("max_price")),
            "entry_price": entry_price,
            "current_buy_price": str(result.get("current_buy_price") or ""),
            "spread": str(result.get("spread") or ""),
            "shares": str(filled),
        },
        "execution": {
            "placed": True,
            "order_id": result.get("order_id"),
            "canceled_remainder": bool(result.get("canceled_remainder", True)),
            "executor": "termux",
        },
    }
    executions = core._load(core.EXECUTIONS_FILE)
    executions[trade_id] = record
    core._save(core.EXECUTIONS_FILE, executions)


def _record_sell_result(queue_rec: dict[str, Any], result: dict[str, Any]) -> None:
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id or not result.get("ok"):
        return
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        return
    q = rec.get("quote") or {}
    sold = Decimal(str(result.get("sold_shares") or "0"))
    remaining = Decimal(str(result.get("remaining_shares") or "0"))
    sell_price = Decimal(str(result.get("sell_price") or "0"))
    entry = Decimal(str(q.get("entry_price") or q.get("limit_price") or "0"))
    realized = Decimal(str(result.get("realized_pnl") or ((sell_price - entry) * sold).quantize(Decimal("0.01"))))
    now = _now_iso()
    rec["status"] = "CLOSED" if remaining <= Decimal("0.0001") else "PARTIALLY_CLOSED"
    rec["exit_price"] = str(sell_price)
    rec["realized_pnl"] = str(realized)
    rec["remaining_shares"] = str(remaining)
    rec["closed_at"] = now if rec["status"] == "CLOSED" else None
    rec.setdefault("execution", {})["close_order_id"] = result.get("order_id")
    sell_id = f"{trade_id}-sell-{uuid.uuid4().hex[:6]}"
    executions[trade_id] = rec
    executions[sell_id] = {
        "id": sell_id,
        "status": "SELL_FILLED",
        "created_at": now,
        "submitted_at": now,
        "side": "SELL",
        "paper": False,
        "source": str(rec.get("source") or "termux_executor"),
        "parent_trade_id": trade_id,
        "display_pnl": str(realized),
        "budget_usdc": str((sell_price * sold).quantize(Decimal("0.01"))),
        "quote": {
            "market": q.get("market"),
            "market_url": q.get("market_url"),
            "market_type": "moneyline",
            "requested_outcome": q.get("requested_outcome"),
            "resolved_outcome": q.get("resolved_outcome"),
            "asset_id": q.get("asset_id"),
            "limit_price": str(sell_price),
            "shares": str(sold),
        },
        "execution": {"placed": True, "order_id": result.get("order_id"), "executor": "termux"},
    }
    core._save(core.EXECUTIONS_FILE, executions)


def _record_already_closed_sell(queue_rec: dict[str, Any], error: str) -> bool:
    """Reconcile a tracked trade when Termux confirms the wallet already has zero shares."""
    marker = "Wallet no longer holds the tracked test shares"
    if marker not in str(error or ""):
        return False
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id:
        return False
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec or rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
        return False
    now = _now_iso()
    rec["status"] = "CLOSED_RECONCILED"
    rec["remaining_shares"] = "0"
    rec["closed_at"] = rec.get("closed_at") or now
    rec["wallet_reconciled_at"] = now
    rec["wallet_position_size"] = "0"
    rec["reconciliation_note"] = (
        "Termux checked the Polymarket wallet before SELL and found zero tracked shares. "
        "The position was already closed outside this SELL request; exit price and realized P/L remain unknown."
    )
    rec.setdefault("execution", {})["close_reconciliation"] = {
        "at": now,
        "executor": "termux",
        "reason": marker,
    }
    executions[trade_id] = rec
    core._save(core.EXECUTIONS_FILE, executions)
    return True


@app.post("/api/executor/result/{request_id}")
def executor_result(request_id: str, body: ExecutorResult, _: dict[str, Any] = Depends(_executor_auth)):
    with _QUEUE_LOCK:
        data = _queue_load()
        rec = data.get(request_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Unknown executor request")
        if rec.get("status") in {"DONE", "FAILED"}:
            return {"ok": True, "duplicate": True}
        result = body.result or {}
        already_closed = bool(
            not body.ok
            and rec.get("action") == "SELL"
            and "Wallet no longer holds the tracked test shares" in str(body.error or "")
        )
        if already_closed:
            payload = rec.get("payload") or {}
            result = {
                "ok": True,
                "status": "CLOSED_RECONCILED",
                "already_closed": True,
                "trade_id": payload.get("trade_id"),
                "message": "Wallet already has zero tracked shares; dashboard position reconciled closed.",
            }
            rec["status"] = "DONE"
            rec["result"] = result
            rec["error"] = None
        else:
            rec["status"] = "DONE" if body.ok else "FAILED"
            rec["result"] = result
            rec["error"] = body.error
        rec["updated_at"] = _now_iso()
        data[request_id] = rec
        _queue_save(data)
    if body.ok and rec.get("action") == "BUY":
        _record_buy_result(rec, result)
    elif body.ok and rec.get("action") == "SELL":
        _record_sell_result(rec, result)
    elif already_closed:
        _record_already_closed_sell(rec, str(body.error or ""))
    return {"ok": True}


def _install_remote_executor_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if "TERMUX EXECUTOR" in html:
        return

    # Wallet reads are now reported by the connected Termux worker rather than by Railway.
    html = html.replace("fetch('/api/live/wallet'", "fetch('/api/executor/wallet'")

    status_box = '''
      <div class="executor-box">
        <div><b>TERMUX EXECUTOR</b> · <span id="executorConnection">Checking…</span></div>
        <div id="executorPairLine" class="executor-pair"></div>
        <div id="executorGeo" class="executor-small"></div>
      </div>
'''
    html = html.replace('<div class="live-test-warning">', status_box + '<div class="live-test-warning">', 1)

    css = '''
.executor-box{border:1px solid var(--border);background:#0b1320;border-radius:12px;padding:12px;margin:12px 0;font-size:12px}.executor-pair{margin-top:7px;font:700 14px ui-monospace,SFMono-Regular,Menlo,monospace}.executor-small{margin-top:5px;color:var(--muted);font-size:11px}.executor-online{color:var(--accent)}.executor-offline{color:var(--bad)}
'''
    html = html.replace('</style>', css + '</style>', 1)

    js = r'''
let remoteExecutorConnected=false;
async function remoteStatus(){
 try{
  const r=await fetch('/api/executor/status',{cache:'no-store'}),d=await r.json();
  remoteExecutorConnected=!!d.connected;
  const c=document.getElementById('executorConnection'),p=document.getElementById('executorPairLine'),g=document.getElementById('executorGeo');
  if(c){c.textContent=d.connected?'CONNECTED':(d.paired?'PAIRED · OFFLINE':'NOT PAIRED');c.className=d.connected?'executor-online':'executor-offline'}
  if(p){p.textContent=d.paired?'Pairing complete':('Pair code: '+(d.pair_code||'—')+' · enter this in Termux')}
  if(g){g.textContent=[d.worker_name?('Worker '+d.worker_name):'',d.geo_country?('Network '+d.geo_country+(d.geo_region?'/'+d.geo_region:'')):'',d.geo_blocked===true?'POLYMARKET BLOCKED':d.geo_blocked===false?'Polymarket geoblock passed':''].filter(Boolean).join(' · ')}
 }catch(e){const c=document.getElementById('executorConnection');if(c){c.textContent='STATUS ERROR';c.className='executor-offline'}}
}
async function remoteRequest(path,payload){
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:payload===undefined?'{}':JSON.stringify(payload)});return await ltJson(r)
}
async function waitRemote(requestId,timeoutMs=90000){
 const started=Date.now();
 while(Date.now()-started<timeoutMs){
  const r=await fetch('/api/executor/request-status/'+encodeURIComponent(requestId),{cache:'no-store'}),d=await ltJson(r);
  if(d.status==='DONE')return d.result||{};
  if(d.status==='FAILED')throw new Error(d.error||'Termux executor failed');
  await new Promise(res=>setTimeout(res,700));
 }
 throw new Error('Timed out waiting for Termux executor. Check that the worker is running before retrying.');
}
function replaceLiveButton(id,handler){const old=document.getElementById(id);if(!old)return;const neo=old.cloneNode(true);old.parentNode.replaceChild(neo,old);neo.addEventListener('click',handler)}
async function remotePreview(){
 try{ltEl('ltMsg').textContent='Sending preview to Termux…';const q=await remoteRequest('/api/executor/request-preview',ltPayload());const d=await waitRemote(q.request_id);ltShow(d);ltEl('ltMsg').textContent='Termux market check passed. No order placed.'}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Termux market check failed.'}
}
async function remoteBuy(){
 try{
  const p=ltPayload();if(!remoteExecutorConnected)throw new Error('Termux executor is not connected');
  if(!confirm(`Queue a REAL $${Number(p.budget_usdc).toFixed(2)} moneyline limit BUY for ${p.outcome} to your Termux executor?`))return;
  ltEl('ltBuy').disabled=true;ltEl('ltMsg').textContent='Waiting for Termux BUY…';const q=await remoteRequest('/api/executor/request-buy',p);const d=await waitRemote(q.request_id);ltShow(d);
  if(d.ok&&q.trade_id){liveTestTradeId=q.trade_id;ltEl('ltSell').disabled=false;ltEl('ltMsg').textContent='Termux BUY filled. SELL is ready.'}else{ltEl('ltMsg').textContent='BUY did not fill; any remainder was canceled.'}
 }catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Termux BUY failed.'}
 finally{ltEl('ltBuy').disabled=false}
}
async function remoteSell(){
 if(!liveTestTradeId){ltShow('No filled Termux test trade is available to sell.');return}
 if(!remoteExecutorConnected){ltShow('Termux executor is not connected.');return}
 if(!confirm('Queue a REAL SELL of the tracked test shares to Termux?'))return;
 try{ltEl('ltSell').disabled=true;ltEl('ltMsg').textContent='Waiting for Termux SELL…';const q=await remoteRequest('/api/executor/request-sell/'+encodeURIComponent(liveTestTradeId));const d=await waitRemote(q.request_id);ltShow(d);ltEl('ltMsg').textContent='Termux SELL completed. Round-trip test finished.';liveTestTradeId=null}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Termux SELL failed; verify the position before retrying.';ltEl('ltSell').disabled=false}
}
replaceLiveButton('ltPreview',remotePreview);replaceLiveButton('ltBuy',remoteBuy);replaceLiveButton('ltSell',remoteSell);
remoteStatus();setInterval(remoteStatus,3000);
'''
    html = html.replace('</script>', js + '\n</script>', 1)
    dashboard.DASHBOARD_HTML = html


_install_remote_executor_ui()
print("TERMUX_EXECUTOR_BRIDGE enabled max_usdc=" + str(REMOTE_MAX_USDC), flush=True)
