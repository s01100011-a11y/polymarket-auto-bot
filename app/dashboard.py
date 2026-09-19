from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from polymarket import PublicClient

from app import main as core

app = core.app
security = HTTPBasic()
STARTED_AT = time.time()
DASHBOARD_SETTINGS_FILE = core.DATA_DIR / "dashboard_settings.json"
AUTO_TRADING_STATE_FILE = core.DATA_DIR / "auto_trading_state.json"
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


class DashboardSettings(BaseModel):
    max_trade_usdc: Decimal = Field(gt=0, le=10000)
    max_auto_trade_usdc: Decimal = Field(gt=0, le=10000)
    max_daily_budget_usdc: Decimal = Field(gt=0, le=100000)
    max_price: Decimal = Field(gt=0, lt=1)
    max_spread: Decimal = Field(ge=0, lt=1)
    auto_poll_seconds: int = Field(ge=10, le=3600)


class AutoTradingState(BaseModel):
    enabled: bool


def _auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="Dashboard password is not configured")
    good_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    good_password = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (good_user and good_password):
        raise HTTPException(status_code=401, detail="Invalid dashboard credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def _current_settings() -> dict:
    return {
        "max_trade_usdc": str(core.MAX_TRADE_USDC),
        "max_auto_trade_usdc": str(core.MAX_AUTO_TRADE_USDC),
        "max_daily_budget_usdc": str(core.MAX_DAILY_BUDGET_USDC),
        "max_price": str(core.MAX_PRICE),
        "max_spread": str(core.MAX_SPREAD),
        "auto_poll_seconds": core.AUTO_POLL_SECONDS,
    }


def _apply_settings(data: dict) -> None:
    core.MAX_TRADE_USDC = Decimal(str(data.get("max_trade_usdc", core.MAX_TRADE_USDC)))
    core.MAX_AUTO_TRADE_USDC = Decimal(str(data.get("max_auto_trade_usdc", core.MAX_AUTO_TRADE_USDC)))
    core.MAX_DAILY_BUDGET_USDC = Decimal(str(data.get("max_daily_budget_usdc", core.MAX_DAILY_BUDGET_USDC)))
    core.MAX_PRICE = Decimal(str(data.get("max_price", core.MAX_PRICE)))
    core.MAX_SPREAD = Decimal(str(data.get("max_spread", core.MAX_SPREAD)))
    core.AUTO_POLL_SECONDS = max(10, int(data.get("auto_poll_seconds", core.AUTO_POLL_SECONDS)))


_saved_settings = core._load(DASHBOARD_SETTINGS_FILE)
if _saved_settings:
    try:
        _apply_settings(_saved_settings)
    except Exception:
        pass

# Dashboard auto-trading state is persistent, but it is intentionally limited
# to paper/order-preparation automation. If live trading is enabled, unattended
# auto execution is forced off and live orders still require explicit approval.
_saved_auto_state = core._load(AUTO_TRADING_STATE_FILE)
# Railway AUTO_TRADING is authoritative. Persisted dashboard state must never
# turn automation off when the Railway variable enables it.
if not core.auto_trading_enabled() and not core.LIVE_TRADING and "enabled" in _saved_auto_state:
    core.AUTO_TRADING = bool(_saved_auto_state.get("enabled"))


def _safe_decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _estimate_pnl(records: list[dict]) -> tuple[list[dict], Decimal]:
    live = []
    total = Decimal("0")
    live_records = [r for r in records if r.get("status") == "ORDER_SUBMITTED"]
    if not live_records:
        return live, total

    try:
        client = PublicClient()
    except Exception:
        client = None

    try:
        for rec in live_records:
            q = rec.get("quote") or {}
            asset_id = q.get("asset_id")
            shares = _safe_decimal(q.get("shares"))
            entry = _safe_decimal(q.get("limit_price"))
            midpoint = None
            pnl = None
            if client and asset_id and shares > 0 and entry > 0:
                try:
                    midpoint = _safe_decimal(client.get_midpoint(asset_id=str(asset_id)))
                    pnl = (midpoint - entry) * shares
                    total += pnl
                except Exception:
                    midpoint = None
                    pnl = None
            live.append({
                "id": rec.get("id"),
                "market": q.get("market") or (rec.get("intent") or {}).get("market_url"),
                "market_url": q.get("market_url") or (rec.get("intent") or {}).get("market_url"),
                "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
                "entry_price": str(entry) if entry else None,
                "current_midpoint": str(midpoint) if midpoint is not None else None,
                "shares": str(shares) if shares else None,
                "budget_usdc": rec.get("budget_usdc"),
                "estimated_pnl": str(pnl.quantize(Decimal("0.01"))) if pnl is not None else None,
                "submitted_at": rec.get("submitted_at") or rec.get("created_at"),
                "order_id": (rec.get("execution") or {}).get("order_id"),
                "status": rec.get("status"),
            })
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass
    return live, total


def _dashboard_snapshot() -> dict:
    watch_map = core._load(core.WATCH_FILE)
    execution_map = core._load(core.EXECUTIONS_FILE)
    executions = list(execution_map.values())
    executions.sort(key=lambda r: r.get("submitted_at") or r.get("created_at") or "", reverse=True)

    watching = []
    for rec in watch_map.values():
        if rec.get("status") in {"WATCHING", "ERROR"}:
            signal = rec.get("signal") or {}
            last = rec.get("last_result") or {}
            quote = last.get("quote") or {}
            watching.append({
                "signal_id": rec.get("signal_id"),
                "status": rec.get("status"),
                "market_url": signal.get("market_url"),
                "outcome": signal.get("outcome"),
                "market_type": signal.get("market_type"),
                "max_price": signal.get("max_price"),
                "budget_usdc": signal.get("budget_usdc"),
                "expires_at": signal.get("expires_at"),
                "best_ask": quote.get("best_ask"),
                "current_buy_price": quote.get("current_buy_price"),
                "last_check_at": rec.get("last_check_at"),
                "last_error": rec.get("last_error"),
            })
    watching.sort(key=lambda r: r.get("last_check_at") or "", reverse=True)

    live_trades, total_pnl = _estimate_pnl(executions)
    dry_runs = [r for r in executions if r.get("status") in {"AUTO_DRY_RUN", "APPROVED_DRY_RUN"}]
    today_used = core._daily_budget_used()

    history = []
    for rec in executions[:100]:
        q = rec.get("quote") or {}
        history.append({
            "id": rec.get("id"),
            "status": rec.get("status"),
            "market": q.get("market") or (rec.get("intent") or {}).get("market_url"),
            "market_url": q.get("market_url") or (rec.get("intent") or {}).get("market_url"),
            "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
            "limit_price": q.get("limit_price"),
            "budget_usdc": rec.get("budget_usdc"),
            "auto": bool(rec.get("auto")),
            "submitted_at": rec.get("submitted_at") or rec.get("created_at"),
            "order_id": (rec.get("execution") or {}).get("order_id"),
        })

    return {
        "status": {
            "ok": True,
            "version": "0.4.0-dashboard",
            "live_trading": core.LIVE_TRADING,
            "auto_trading": core.auto_trading_enabled(),
            "block_political_auto": core.BLOCK_POLITICAL_AUTO,
            "uptime_seconds": int(time.time() - STARTED_AT),
            "poll_seconds": core.AUTO_POLL_SECONDS,
            "active_watches": len(watching),
            "submitted_live_trades": len(live_trades),
            "dry_run_executions": len(dry_runs),
            "daily_budget_used": str(today_used),
            "max_daily_budget_usdc": str(core.MAX_DAILY_BUDGET_USDC),
            "estimated_total_pnl": str(total_pnl.quantize(Decimal("0.01"))),
            "pnl_note": "Live positions use Polymarket wallet cost basis and unrealized P/L when available; paper positions use their captured entry snapshot.",
        },
        "watching": watching,
        "live_trades": live_trades,
        "history": history,
        "settings": _current_settings(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# Remove the old simple root page so the dashboard becomes the service homepage.
app.router.routes = [
    route for route in app.router.routes
    if not (getattr(route, "path", None) == "/" and "GET" in (getattr(route, "methods", set()) or set()))
]


@app.get("/api/dashboard", dependencies=[Depends(_auth)])
def dashboard_api():
    return _dashboard_snapshot()


@app.get("/api/dashboard/settings", dependencies=[Depends(_auth)])
def dashboard_settings_get():
    return {
        "editable": _current_settings(),
        "read_only": {
            "live_trading": core.LIVE_TRADING,
            "auto_trading": core.auto_trading_enabled(),
            "block_political_auto": core.BLOCK_POLITICAL_AUTO,
        },
    }


@app.put("/api/dashboard/settings", dependencies=[Depends(_auth)])
def dashboard_settings_put(settings: DashboardSettings):
    if settings.max_auto_trade_usdc > settings.max_daily_budget_usdc:
        raise HTTPException(status_code=400, detail="Per-trade auto cap cannot exceed the daily budget cap")
    data = settings.model_dump(mode="json")
    _apply_settings(data)
    core._save(DASHBOARD_SETTINGS_FILE, data)
    return {"ok": True, "settings": _current_settings()}


@app.get("/api/dashboard/auto-trading", dependencies=[Depends(_auth)])
def dashboard_auto_trading_get():
    return {
        "enabled": bool(core.auto_trading_enabled()),
        "live_trading": bool(core.LIVE_TRADING),
        "can_enable": not bool(core.LIVE_TRADING),
        "scope": "paper_and_order_preparation",
    }


@app.put("/api/dashboard/auto-trading", dependencies=[Depends(_auth)])
def dashboard_auto_trading_put(state: AutoTradingState):
    if state.enabled and core.LIVE_TRADING:
        raise HTTPException(
            status_code=409,
            detail="Auto Trading can only be enabled for paper/order-preparation mode while live trading is active.",
        )
    core.AUTO_TRADING = bool(state.enabled)
    core._save(AUTO_TRADING_STATE_FILE, {"enabled": bool(core.AUTO_TRADING)})
    return {
        "ok": True,
        "enabled": bool(core.AUTO_TRADING),
        "live_trading": bool(core.LIVE_TRADING),
        "can_enable": not bool(core.LIVE_TRADING),
        "scope": "paper_and_order_preparation",
    }


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#090d14;--panel:#111827;--panel2:#0f172a;--border:#263244;--text:#f3f6fb;--muted:#94a3b8;--accent:#6ee7b7;--warn:#fbbf24;--bad:#fb7185;--blue:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#08101d 0,#090d14 300px);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
a{color:#8bc4ff;text-decoration:none}.wrap{max-width:1260px;margin:auto;padding:28px 18px 60px}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:22px}.eyebrow{font-size:12px;color:var(--accent);letter-spacing:.14em;text-transform:uppercase;font-weight:800}.title{font-size:31px;font-weight:850;margin:5px 0 2px}.sub{color:var(--muted);font-size:14px}.badge{display:inline-flex;align-items:center;gap:7px;padding:8px 11px;border:1px solid var(--border);background:#0b1220;border-radius:999px;font-size:12px;font-weight:750}.dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}.cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:18px 0}.card,.panel{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.16)}.card{padding:16px}.label{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:800}.value{font-size:24px;font-weight:850;margin-top:7px}.value.small{font-size:17px}.green{color:var(--accent)}.red{color:var(--bad)}.yellow{color:var(--warn)}.tabs{display:flex;gap:8px;margin:22px 0 12px;flex-wrap:wrap}.tab{border:1px solid var(--border);background:#0d1522;color:var(--muted);padding:9px 13px;border-radius:10px;font-weight:750;cursor:pointer}.tab.active{color:#fff;border-color:#3d4d67;background:#172033}.panel{padding:18px;display:none}.panel.active{display:block}.panel h2{margin:0 0 4px;font-size:19px}.note{color:var(--muted);font-size:12px;margin:0 0 14px}.table-wrap{overflow:auto;border:1px solid var(--border);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:850px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #202b3b;font-size:13px;vertical-align:top}th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.09em;background:#0d1522}tr:last-child td{border-bottom:0}.status{font-size:11px;font-weight:850;padding:4px 7px;border-radius:8px;background:#172033;border:1px solid #31415b;white-space:nowrap}.empty{padding:28px;text-align:center;color:var(--muted)}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.setting{padding:14px;border:1px solid var(--border);border-radius:12px;background:#0d1522}.setting label{display:block;color:var(--muted);font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}.setting input{width:100%;background:#080e18;border:1px solid #344157;color:#fff;border-radius:9px;padding:10px;font:inherit}.readonly{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}.ro{padding:12px;border:1px solid var(--border);border-radius:12px;background:#0d1522}.ro b{display:block;font-size:13px;margin-top:4px}.toggle-btn{margin-top:9px;width:100%;border:1px solid #475569;background:#172033;color:#fff;padding:9px 11px;border-radius:9px;font-weight:850;cursor:pointer}.toggle-btn.active{background:#12362d;border-color:#2e7d64;color:#a7f3d0}.toggle-btn:disabled{opacity:.45;cursor:not-allowed}.toggle-note{display:block;margin-top:7px;color:var(--muted);font-size:10px;line-height:1.35}.actions{display:flex;align-items:center;gap:10px;margin-top:14px}.btn{border:0;background:#d1fae5;color:#07251c;padding:10px 15px;border-radius:10px;font-weight:850;cursor:pointer}.save-msg{font-size:12px;color:var(--muted)}.foot{color:var(--muted);font-size:11px;margin-top:16px}.market{max-width:320px}.muted{color:var(--muted)}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid2,.readonly{grid-template-columns:1fr}.top{flex-direction:column}.title{font-size:27px}}
@media(max-width:520px){.cards{grid-template-columns:1fr 1fr}.value{font-size:20px}.wrap{padding:20px 12px 48px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><div class="eyebrow">Railway · Polymarket</div><div class="title">Trading Bot Dashboard</div><div class="sub" id="updated">Loading status…</div></div>
    <div class="badge"><span class="dot"></span><span id="serviceState">Connecting</span></div>
  </div>
  <div class="cards">
    <div class="card"><div class="label">Mode</div><div class="value small" id="mode">—</div></div>
    <div class="card"><div class="label">Active watches</div><div class="value" id="watches">—</div></div>
    <div class="card"><div class="label">Live trades</div><div class="value" id="liveTrades">—</div></div>
    <div class="card"><div class="label">Est. P/L</div><div class="value" id="pnl">—</div></div>
    <div class="card"><div class="label">Daily budget used</div><div class="value small" id="budget">—</div></div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="current">Current</button>
    <button class="tab" data-tab="history">Trade history</button>
    <button class="tab" data-tab="settings">Configuration</button>
  </div>

  <section class="panel active" id="current">
    <h2>Current watches & trades</h2><p class="note">Price watches refresh in the bot loop. Live positions show Polymarket wallet cost basis and unrealized P/L when available.</p>
    <div id="currentBody"></div>
  </section>
  <section class="panel" id="history">
    <h2>Trade history</h2><p class="note">Includes live submissions and dry-run executions recorded by this bot.</p>
    <div id="historyBody"></div>
  </section>
  <section class="panel" id="settings">
    <h2>Configuration</h2><p class="note">Risk and polling controls can be changed here. Wallet secrets and master live-trading switches stay in Railway.</p>
    <div class="readonly">
      <div class="ro"><span class="label">Live trading</span><b id="roLive">—</b></div>
      <div class="ro"><span class="label">Auto trading</span><b id="roAuto">—</b><button type="button" class="toggle-btn" id="autoTradingBtn">Loading…</button><span class="toggle-note" id="autoTradingNote">Controls paper trading and automatic order preparation.</span></div>
      <div class="ro"><span class="label">Political auto</span><b id="roPolitics">BLOCKED</b></div>
    </div>
    <form id="settingsForm"><div class="grid2">
      <div class="setting"><label>Manual trade cap (USDC)</label><input id="max_trade_usdc" type="number" step="0.01" min="0.01"></div>
      <div class="setting"><label>Auto trade cap (USDC)</label><input id="max_auto_trade_usdc" type="number" step="0.01" min="0.01"></div>
      <div class="setting"><label>Daily auto budget (USDC)</label><input id="max_daily_budget_usdc" type="number" step="0.01" min="0.01"></div>
      <div class="setting"><label>Maximum buy price</label><input id="max_price" type="number" step="0.01" min="0.01" max="0.99"></div>
      <div class="setting"><label>Maximum spread</label><input id="max_spread" type="number" step="0.01" min="0" max="0.99"></div>
      <div class="setting"><label>Watch poll interval (seconds)</label><input id="auto_poll_seconds" type="number" step="1" min="10" max="3600"></div>
    </div><div class="actions"><button class="btn" type="submit">Save settings</button><span class="save-msg" id="saveMsg"></span></div></form>
  </section>
  <div class="foot" id="pnlNote"></div>
</div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>v===null||v===undefined||v===''?'—':'$'+Number(v).toFixed(2);
const price=v=>v===null||v===undefined||v===''?'—':Number(v).toFixed(3);
const when=v=>v?new Date(v).toLocaleString():'—';
function table(headers,rows){if(!rows.length)return '<div class="empty">Nothing here yet.</div>';return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function link(url,label){return url?`<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(label||url)}</a>`:esc(label||'—')}
function setPnl(el,v){const n=Number(v||0);el.textContent=money(n);el.className='value '+(n>0?'green':n<0?'red':'')}
async function load(){
 try{
  const r=await fetch('/api/dashboard',{cache:'no-store'}); if(!r.ok)throw new Error('HTTP '+r.status); const d=await r.json(),s=d.status;
  document.getElementById('serviceState').textContent='Online'; document.getElementById('updated').textContent='Updated '+new Date(d.generated_at).toLocaleTimeString()+' · uptime '+Math.floor(s.uptime_seconds/60)+'m';
  document.getElementById('mode').textContent=(s.live_trading?'LIVE':'DRY RUN')+' · Auto '+(s.auto_trading?'ON':'OFF'); document.getElementById('watches').textContent=s.active_watches; document.getElementById('liveTrades').textContent=s.submitted_live_trades; setPnl(document.getElementById('pnl'),s.estimated_total_pnl); document.getElementById('budget').textContent=money(s.daily_budget_used)+' / '+money(s.max_daily_budget_usdc); document.getElementById('pnlNote').textContent=s.pnl_note;
  document.getElementById('roLive').textContent=s.live_trading?'ENABLED':'DISABLED'; document.getElementById('roAuto').textContent=s.auto_trading?'ENABLED':'DISABLED'; document.getElementById('roPolitics').textContent=s.block_political_auto?'BLOCKED':'UNBLOCKED';
  const autoBtn=document.getElementById('autoTradingBtn'),autoNote=document.getElementById('autoTradingNote'); autoBtn.dataset.enabled=s.auto_trading?'1':'0'; autoBtn.textContent=s.auto_trading?'AUTO TRADING ON':'AUTO TRADING OFF'; autoBtn.classList.toggle('active',!!s.auto_trading); autoBtn.disabled=!!(s.live_trading&&!s.auto_trading); autoNote.textContent=s.live_trading?'Live mode is active: this control cannot enable unattended real-money execution.':'Controls paper trading and automatic order preparation.';
  const wr=d.watching.map(x=>`<tr><td><span class="status">${esc(x.status)}</span></td><td class="market">${link(x.market_url,x.outcome||x.market_url)}</td><td>${esc(x.market_type||'—')}</td><td>${price(x.best_ask||x.current_buy_price)}</td><td>${price(x.max_price)}</td><td>${money(x.budget_usdc)}</td><td>${when(x.expires_at)}</td><td class="muted">${esc(x.last_error||'')}</td></tr>`);
  const lr=d.live_trades.map(x=>`<tr><td><span class="status">LIVE</span></td><td class="market">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.entry_price)}</td><td>${price(x.current_midpoint)}</td><td>${money(x.budget_usdc)}</td><td class="${Number(x.estimated_pnl)>0?'green':Number(x.estimated_pnl)<0?'red':''}">${money(x.estimated_pnl)}</td><td>${when(x.submitted_at)}</td></tr>`);
  document.getElementById('currentBody').innerHTML=(d.watching.length?'<div class="label" style="margin:8px 0">Price watches</div>':'')+table(['Status','Outcome / market','Type','Current','Limit','Budget','Expires','Error'],wr)+(d.live_trades.length?'<div class="label" style="margin:18px 0 8px">Submitted live trades</div>':'')+table(['Status','Market','Outcome','Entry','Current','Cost','Live P/L','Submitted'],lr);
  const hr=d.history.map(x=>`<tr><td><span class="status">${esc(x.status||'—')}</span></td><td class="market">${link(x.market_url,x.market)}</td><td>${esc(x.outcome||'—')}</td><td>${price(x.limit_price)}</td><td>${money(x.budget_usdc)}</td><td>${x.auto?'Auto':'Manual'}</td><td>${when(x.submitted_at)}</td><td class="muted">${esc(x.order_id||'—')}</td></tr>`);
  document.getElementById('historyBody').innerHTML=table(['Status','Market','Outcome','Limit','Budget','Source','Time','Order ID'],hr);
  Object.entries(d.settings).forEach(([k,v])=>{const e=document.getElementById(k);if(e&&!e.dataset.dirty)e.value=v});
 }catch(e){document.getElementById('serviceState').textContent='Dashboard error';document.getElementById('updated').textContent=String(e)}
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active')});
document.querySelectorAll('#settingsForm input').forEach(e=>e.addEventListener('input',()=>e.dataset.dirty='1'));
document.getElementById('autoTradingBtn').onclick=async()=>{const b=document.getElementById('autoTradingBtn'),want=b.dataset.enabled!=='1';b.disabled=true;try{const r=await fetch('/api/dashboard/auto-trading',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:want})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Auto Trading update failed');await load()}catch(err){alert('Auto Trading update failed: '+err.message);await load()}};
document.getElementById('settingsForm').onsubmit=async e=>{e.preventDefault();const ids=['max_trade_usdc','max_auto_trade_usdc','max_daily_budget_usdc','max_price','max_spread','auto_poll_seconds'];const body={};ids.forEach(id=>body[id]=id==='auto_poll_seconds'?Number(document.getElementById(id).value):document.getElementById(id).value);const m=document.getElementById('saveMsg');m.textContent='Saving…';try{const r=await fetch('/api/dashboard/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Save failed');m.textContent='Saved';ids.forEach(id=>delete document.getElementById(id).dataset.dirty);await load()}catch(err){m.textContent='Error: '+err.message}};
load();setInterval(load,15000);
</script>
</body></html>'''


@app.get("/", response_class=HTMLResponse)
def dashboard_home(_: str = Depends(_auth)):
    return DASHBOARD_HTML


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_alias(_: str = Depends(_auth)):
    return DASHBOARD_HTML
