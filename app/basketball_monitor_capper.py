from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException

from app import capper_control

_INSTALLED = False

SPORTS = {
    "WNBA": {
        "label": "WNBA Monitor - WNBA",
        "state_file": "pw_export_ingest_state.json",
    },
    "NBA": {
        "label": "NBA Monitor - NBA",
        "state_file": "pw_nba_export_ingest_state.json",
    },
}

DEFAULT_UNIT_USDC = Decimal("10")


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _alert_sport(alert: dict[str, Any], ingest: Any) -> str | None:
    direct = str(
        alert.get("pw_export_sport")
        or (alert.get("pw_export_meta") or {}).get("sport")
        or ""
    ).upper()
    if direct in SPORTS:
        return direct

    text = str(alert.get("text") or "").lstrip().upper()
    if text.startswith("WNBA PW ALERT"):
        return "WNBA"
    if text.startswith("NBA PW ALERT"):
        return "NBA"

    event_id = str(alert.get("event_id") or "")
    if not event_id.startswith("pwexport-"):
        return None
    parsed = alert.get("parsed") or {}
    selection = (
        parsed.get("selection")
        or (parsed.get("pw") or {}).get("predicted_winner")
        or (alert.get("pw_export_meta") or {}).get("predicted_winner")
    )
    league = ingest._league_for_team(str(selection or ""))
    return str(league or "").upper() if str(league or "").upper() in SPORTS else None


def _execution_sport(rec: dict[str, Any], ingest: Any) -> str | None:
    direct = str(rec.get("strategy_sport") or "").upper()
    if direct in SPORTS:
        return direct

    event_id = str(rec.get("slack_event_id") or rec.get("strategy_pick_id") or "")
    if not event_id.startswith("pwexport-"):
        return None

    decision = rec.get("pw_strategy_decision") or {}
    quote = rec.get("quote") or {}
    selection = (
        rec.get("strategy_selection")
        or decision.get("predicted_winner")
        or quote.get("requested_outcome")
        or quote.get("resolved_outcome")
    )
    league = ingest._league_for_team(str(selection or ""))
    return str(league or "").upper() if str(league or "").upper() in SPORTS else None


def _normalized_executions(executions: dict[str, Any], ingest: Any) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for execution_id, raw in executions.items():
        if not isinstance(raw, dict):
            normalized[execution_id] = raw
            continue
        rec = dict(raw)
        sport = _execution_sport(rec, ingest)
        if sport:
            rec["strategy_sport"] = sport
            rec["strategy_source"] = f"{sport} Monitor - {sport}"
            rec["strategy_pick_id"] = rec.get("strategy_pick_id") or rec.get("slack_event_id")
            if not rec.get("strategy_selection"):
                rec["strategy_selection"] = (
                    (rec.get("pw_strategy_decision") or {}).get("predicted_winner")
                    or (rec.get("quote") or {}).get("requested_outcome")
                    or (rec.get("quote") or {}).get("resolved_outcome")
                )
        normalized[execution_id] = rec
    return normalized


def _signal_items(
    alerts: dict[str, Any],
    executions: dict[str, Any],
    *,
    sport: str,
    ingest: Any,
    limit: int = 80,
) -> list[dict[str, Any]]:
    execution_by_signal: dict[str, dict[str, Any]] = {}
    for execution_id, rec in executions.items():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        signal_id = str(rec.get("strategy_pick_id") or rec.get("slack_event_id") or "")
        if not signal_id:
            continue
        current = execution_by_signal.get(signal_id)
        if current is None or str(rec.get("submitted_at") or "") > str(current.get("submitted_at") or ""):
            execution_by_signal[signal_id] = {**rec, "_execution_id": execution_id}

    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for key, raw in alerts.items():
        if not isinstance(raw, dict) or _alert_sport(raw, ingest) != sport:
            continue
        event_id = str(raw.get("event_id") or key)
        parsed = raw.get("parsed") or {}
        pw = parsed.get("pw") or {}
        meta = raw.get("pw_export_meta") or {}
        decision = raw.get("pw_strategy") or {}
        posted_at = meta.get("event_ts") or pw.get("event_ts") or raw.get("received_at")
        posted = _parse_dt(posted_at)
        age_seconds = max(0, int((now - posted).total_seconds())) if posted else None
        execution = execution_by_signal.get(event_id)
        settlement = (execution or {}).get("settlement") or {}
        trade_result = str(settlement.get("result") or "").upper() or None
        rows.append(
            {
                "signal_id": event_id,
                "sport": sport,
                "posted_at": posted_at,
                "received_at": raw.get("received_at"),
                "signal_age_seconds": age_seconds,
                "selection": parsed.get("selection") or meta.get("predicted_winner") or decision.get("predicted_winner"),
                "game_id": meta.get("game_id") or pw.get("game_id") or decision.get("game_id"),
                "quarter": meta.get("quarter") or pw.get("quarter") or decision.get("quarter"),
                "win_probability": meta.get("win_probability") or pw.get("win_probability") or decision.get("win_probability"),
                "bk_ml": meta.get("bk_ml") or decision.get("bk_ml"),
                "score": meta.get("score_at_alert") or parsed.get("score"),
                "status": raw.get("status"),
                "error": raw.get("error"),
                "strategy_action": decision.get("action"),
                "strategy_reason": decision.get("reason"),
                "matched_strategies": decision.get("matched_strategies") or [],
                "trade_id": str((execution or {}).get("id") or (execution or {}).get("_execution_id") or "") or raw.get("paper_trade_id") or raw.get("live_trade_id"),
                "trade_status": (execution or {}).get("status"),
                "trade_result": trade_result,
                "trade_pnl_usdc": (execution or {}).get("realized_pnl"),
                "trade_stake_usdc": (execution or {}).get("actual_cost_usdc") or (execution or {}).get("budget_usdc"),
                "trade_executed": execution is not None,
            }
        )

    rows.sort(key=lambda row: str(row.get("posted_at") or row.get("received_at") or ""), reverse=True)
    return rows[:limit]


def install(*, app: Any, dashboard: Any, core: Any, ingest: Any, nfl: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    @app.get("/api/basketball-monitor/status", dependencies=[Depends(dashboard._auth)])
    def basketball_monitor_status():
        alert_map = core._load(ingest.SLACK_ALERTS_FILE)
        alerts = alert_map if isinstance(alert_map, dict) else {}
        raw_executions = core._load(core.EXECUTIONS_FILE)
        raw_executions = raw_executions if isinstance(raw_executions, dict) else {}
        executions = _normalized_executions(raw_executions, ingest)
        live_marks = nfl._live_mark_map(dashboard, executions)

        cappers: dict[str, Any] = {}
        for sport, spec in SPORTS.items():
            label = spec["label"]
            unit_config = nfl._capper_unit_config(core, label, DEFAULT_UNIT_USDC)
            stats = nfl._stats_from_executions(
                executions,
                labels=(label,),
                sport=sport,
                live_marks=live_marks,
            ).get(label, {})
            positions = nfl._position_items(
                executions,
                label,
                sport=sport,
                limit=60,
                live_marks=live_marks,
            )
            signals = _signal_items(
                alerts,
                executions,
                sport=sport,
                ingest=ingest,
            )
            feed_raw = core._load(core.DATA_DIR / spec["state_file"])
            feed = feed_raw if isinstance(feed_raw, dict) else {}
            cappers[label] = {
                **stats,
                "sport": sport,
                "source": f"{sport} Monitor",
                "unit_usdc": unit_config["unit_usdc"],
                "fixed_unit_usdc": unit_config["fixed_unit_usdc"],
                "unit_mode": unit_config["mode"],
                "portfolio_pct": unit_config["portfolio_pct"],
                "portfolio_value_usdc": unit_config["portfolio_value_usdc"],
                "unit_error": unit_config["error"],
                "sizing_mode": "TO_WIN",
                "enabled": capper_control.is_enabled(core, label),
                "signals": signals,
                "positions": positions,
                "feed": {
                    "last_success_at": feed.get("last_success_at"),
                    "last_poll_at": feed.get("last_poll_at"),
                    "last_poll_records": feed.get("last_poll_records"),
                    "last_record_status": feed.get("last_record_status"),
                    "last_record_error": feed.get("last_record_error"),
                    "consecutive_errors": feed.get("consecutive_errors") or 0,
                    "successful_polls": feed.get("successful_polls") or 0,
                    "last_route": feed.get("last_route"),
                },
            }

        online_count = sum(1 for row in cappers.values() if row.get("enabled", True))
        auto_trading = bool(core.auto_trading_enabled())
        live_trading = bool(core.live_trading_enabled())
        return {
            "cappers": cappers,
            "online_count": online_count,
            "auto_trading": auto_trading,
            "live_trading": live_trading,
            "auto_live": bool(online_count and auto_trading and live_trading),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _monitor_label(sport_key: str) -> str:
        sport = str(sport_key or "").strip().upper()
        spec = SPORTS.get(sport)
        if spec is None:
            raise HTTPException(status_code=404, detail="Unknown basketball monitor sport")
        return str(spec["label"])

    @app.put("/api/basketball-monitor/enabled/{sport_key}", dependencies=[Depends(dashboard._auth)])
    def basketball_monitor_enabled(sport_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = _monitor_label(sport_key)
        if not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        enabled_now = capper_control.set_enabled(core, label, payload["enabled"])
        return {
            "ok": True,
            "capper": label,
            "enabled": enabled_now,
            "note": "Controls new automatic monitor trades only; existing queued/open positions are unchanged.",
        }

    @app.put("/api/basketball-monitor/unit-size/{sport_key}", dependencies=[Depends(dashboard._auth)])
    def basketball_monitor_unit_size(sport_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = _monitor_label(sport_key)
        try:
            nfl._set_capper_unit_usdc(core, label, payload.get("unit_usdc"))
            config = nfl._capper_unit_config(core, label, DEFAULT_UNIT_USDC)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "capper": label,
            **config,
            "sizing_mode": "TO_WIN",
            "note": "Fixed mode enabled. Applies to new monitor trades only; existing queued/open positions are unchanged.",
        }

    @app.put("/api/basketball-monitor/unit-percent/{sport_key}", dependencies=[Depends(dashboard._auth)])
    def basketball_monitor_unit_percent(sport_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = _monitor_label(sport_key)
        try:
            nfl._set_capper_portfolio_pct(core, label, payload.get("portfolio_pct", 10))
            config = nfl._capper_unit_config(core, label, DEFAULT_UNIT_USDC)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "capper": label,
            **config,
            "sizing_mode": "TO_WIN",
            "note": "Portfolio-percentage mode enabled. 1u recalculates from total wallet value before each new monitor order.",
        }

    html = dashboard.DASHBOARD_HTML
    if 'id="basketballMonitorStats"' in html:
        return

    panel = r"""
  <div id="basketballMonitorStats">
    <div class="nfl-capper-panel" id="wnbaAutoTradingPanel">
      <div class="nfl-capper-head">
        <div class="capper-panel-title">WNBA AUTO-TRADING</div>
      </div>
      <div class="capper-panel-status-row">
        <div class="capper-status-field"><div class="capper-status-label">STATUS</div><div class="nfl-capper-state" id="wnbaMonitorState">Loading…</div></div>
        <button type="button" class="badge capper-power-btn sport-power-btn" id="monitorCapperPower-wnba" data-enabled="1" aria-pressed="true" onclick="monitorToggleCapper('wnba',this)" title="Toggle WNBA automatic trading"><span class="dot"></span><span class="capper-power-text">Online</span></button>
      </div>
      <div class="capper-panel-actions"><button type="button" data-capper-last24h-toggle onclick="capperToggleLast24h()">Last 24h only: OFF</button></div>
      <div class="nfl-capper-grid single-capper-grid">
        <div class="nfl-capper-card"><div class="capper-card-head"><b>WNBA Monitor</b></div><div class="nfl-capper-kpis" id="wnbaMonitorCard">—</div></div>
      </div>
    </div>
    <div class="nfl-capper-panel" id="nbaAutoTradingPanel">
      <div class="nfl-capper-head">
        <div class="capper-panel-title">NBA AUTO-TRADING</div>
      </div>
      <div class="capper-panel-status-row">
        <div class="capper-status-field"><div class="capper-status-label">STATUS</div><div class="nfl-capper-state" id="nbaMonitorState">Loading…</div></div>
        <button type="button" class="badge capper-power-btn sport-power-btn" id="monitorCapperPower-nba" data-enabled="1" aria-pressed="true" onclick="monitorToggleCapper('nba',this)" title="Toggle NBA automatic trading"><span class="dot"></span><span class="capper-power-text">Online</span></button>
      </div>
      <div class="capper-panel-actions"><button type="button" data-capper-last24h-toggle onclick="capperToggleLast24h()">Last 24h only: OFF</button></div>
      <div class="nfl-capper-grid single-capper-grid">
        <div class="nfl-capper-card"><div class="capper-card-head"><b>NBA Monitor</b></div><div class="nfl-capper-kpis" id="nbaMonitorCard">—</div></div>
      </div>
    </div>
  </div>
"""
    html = html.replace('  <div class="tabs">', panel + '  <div class="tabs">', 1)

    css = r"""
.single-capper-grid{grid-template-columns:1fr!important}.monitor-performance{font-size:14px;line-height:1.7;margin:3px 0 9px}.monitor-feed{font-size:11px;margin:5px 0 9px}.monitor-signal{margin-top:7px;padding:8px;border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.10)}.monitor-signal b{font-size:14px}.monitor-meta{font-size:11px;line-height:1.5;margin-top:3px}.monitor-action{font-size:12px;font-weight:850;margin-top:4px}.monitor-empty{margin-top:7px;opacity:.7}
"""
    html = html.replace("</style>", css + "</style>", 1)

    js = r"""
function monitorEsc(v){
 return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function monitorMoney(v){
 if(v===null||v===undefined||v==='')return '—';
 const n=Number(v||0);
 return (n>0?'+':'')+'$'+n.toFixed(2);
}
function monitorPnlClass(v){
 const n=Number(v||0);
 return n>0?'positive':n<0?'negative':'flat';
}
function monitorTime(v){
 if(!v)return '—';
 const d=new Date(v);
 return Number.isNaN(d.getTime())?monitorEsc(v):monitorEsc(d.toLocaleString());
}
let basketballMonitorData={};

function monitorPositions(x){
 const items=Array.isArray(x.positions)?x.positions:[];
 const open=items.filter(item=>item.sell_available);
 const settledAll=items.filter(item=>!item.sell_available&&item.finished);
 const settled=capperLast24hOnly
  ? settledAll.filter(item=>capperWithin24h(item.closed_at||item.submitted_at))
  : settledAll;

 const openHtml=open.length?open.map(item=>{
  const raw=item.live_pnl_usdc===null||item.live_pnl_usdc===undefined?null:Number(item.live_pnl_usdc);
  const entry=item.entry_price===null||item.entry_price===undefined?'—':(Number(item.entry_price)*100).toFixed(1)+'¢';
  const live=item.current_price===null||item.current_price===undefined?'—':(Number(item.current_price)*100).toFixed(1)+'¢';
  const pnl=raw===null?'—':monitorMoney(raw)+(item.live_pnl_pct===null||item.live_pnl_pct===undefined?'':' ('+Number(item.live_pnl_pct).toFixed(1)+'%)');
  const sell=item.trade_id?'<button type="button" style="margin-top:5px" data-trade-id="'+monitorEsc(item.trade_id)+'" onclick="monitorSell(this.dataset.tradeId,this)">SELL POSITION</button>':'';
  return '<div class="nfl-position-row open"><div class="nfl-position-title">'+monitorEsc(item.selection||item.market||'Position')+' · OPEN</div><div>'+monitorEsc(item.outcome||'')+' · Entry '+entry+' · Live '+live+'</div><div class="nfl-position-pnl '+monitorPnlClass(raw)+'">Live P/L '+pnl+'</div>'+sell+'</div>';
 }).join(''):'<div class="monitor-empty">No open positions.</div>';

 let html='<div style="margin-top:9px"><span class="capper-section-badge">Open positions</span>'+openHtml+'</div>';
 if(settled.length||capperLast24hOnly){
  const settledHtml=settled.length?settled.map(item=>{
   const result=String(item.result||'').toUpperCase();
   const awaiting=!result&&String(item.status||'').toUpperCase()==='CLOSED_RECONCILED';
   const raw=item.realized_pnl_usdc===null||item.realized_pnl_usdc===undefined?null:Number(item.realized_pnl_usdc);
   const pnl=raw===null?'':('<div class="nfl-position-pnl '+monitorPnlClass(raw)+'">Realized P/L '+monitorMoney(raw)+'</div>');
   const manual=awaiting&&item.trade_id?'<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:5px"><button type="button" data-result="WIN" data-trade-id="'+monitorEsc(item.trade_id)+'" onclick="monitorSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE WIN</button><button type="button" data-result="LOSS" data-trade-id="'+monitorEsc(item.trade_id)+'" onclick="monitorSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE LOSS</button><button type="button" data-result="PUSH" data-trade-id="'+monitorEsc(item.trade_id)+'" onclick="monitorSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE PUSH</button></div>':'';
   const label=result?' · '+result:(awaiting?' · AWAITING SETTLEMENT':'');
   return '<div class="nfl-position-row '+(result==='WIN'?'win':result==='LOSS'?'loss':result==='PUSH'?'push':'')+'"><div class="nfl-position-title">'+monitorEsc(item.selection||item.market||'Position')+label+'</div><div>'+monitorEsc(item.outcome||'')+(item.stake_usdc?' · Stake $'+Number(item.stake_usdc).toFixed(2):'')+'</div>'+pnl+manual+'</div>';
  }).join(''):'<div class="monitor-empty">No settled positions in the last 24 hours.</div>';
  html+='<div style="margin-top:12px"><span class="capper-section-badge">Settled positions'+(capperLast24hOnly?' · last 24h':'')+'</span>'+settledHtml+'</div>';
 }
 return html;
}

function monitorSignals(x){
 let items=Array.isArray(x.signals)?x.signals:[];
 if(capperLast24hOnly)items=items.filter(item=>capperWithin24h(item.posted_at||item.received_at));
 if(!items.length)return '<div style="margin-top:12px"><span class="capper-section-badge">Signals'+(capperLast24hOnly?' · last 24h':'')+'</span><div class="monitor-empty">No monitor signals in this window.</div></div>';
 const rows=items.map(item=>{
  const meta=[];
  if(item.posted_at)meta.push('Signal '+monitorTime(item.posted_at));
  if(item.quarter)meta.push(monitorEsc(item.quarter));
  if(item.win_probability!==null&&item.win_probability!==undefined)meta.push('PW '+Number(item.win_probability).toFixed(1)+'%');
  if(item.bk_ml!==null&&item.bk_ml!==undefined)meta.push('BK ML '+(Number(item.bk_ml)>0?'+':'')+Number(item.bk_ml));
  if(item.score)meta.push('Score '+monitorEsc(item.score));
  if(item.status)meta.push('status '+monitorEsc(item.status));
  const action=item.strategy_action?item.strategy_action+(item.strategy_reason?' · '+item.strategy_reason:''):(item.error||'');
  let trade='';
  if(item.trade_executed){
   trade='<div class="monitor-action '+monitorPnlClass(item.trade_pnl_usdc)+'">Trade '+monitorEsc(item.trade_status||'tracked')+(item.trade_result?' · '+monitorEsc(item.trade_result):'')+(item.trade_pnl_usdc!==null&&item.trade_pnl_usdc!==undefined?' · P/L '+monitorMoney(item.trade_pnl_usdc):'')+'</div>';
  }else if(item.status==='NO_TRADE'||item.strategy_action==='PASS'){
   trade='<div class="monitor-action flat">NOT TRADED</div>';
  }
  return '<div class="monitor-signal"><b>'+monitorEsc(item.selection||'Unknown selection')+'</b><div class="monitor-meta">'+meta.join(' · ')+'</div>'+(action?'<div class="monitor-meta">'+monitorEsc(action)+'</div>':'')+trade+'</div>';
 }).join('');
 return '<div style="margin-top:12px"><span class="capper-section-badge">Signals'+(capperLast24hOnly?' · last 24h':'')+'</span>'+rows+'</div>';
}

async function monitorSetUnitSize(sportKey,btn){
 const input=document.getElementById('monitorUnitSize-'+sportKey);
 const value=Number(input&&input.value);
 if(!Number.isFinite(value)||value<=0){
  alert('Enter a unit size greater than 0.');
  return;
 }
 const original=btn.textContent;btn.disabled=true;btn.textContent='SAVING…';
 try{
  const r=await fetch('/api/basketball-monitor/unit-size/'+encodeURIComponent(sportKey),{
   method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({unit_usdc:value})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unit size update failed');
  btn.textContent='SAVED';
  await loadBasketballMonitors();
 }catch(e){btn.disabled=false;btn.textContent=original;alert(String(e.message||e))}
}
async function monitorSetPortfolioPct(sportKey,btn){
 const input=document.getElementById('monitorPortfolioPct-'+sportKey);
 const value=Number(input&&input.value);
 if(!Number.isFinite(value)||value<=0||value>100){
  alert('Enter a portfolio percentage greater than 0 and no more than 100.');
  return;
 }
 const original=btn.textContent;btn.disabled=true;btn.textContent='SAVING…';
 try{
  const r=await fetch('/api/basketball-monitor/unit-percent/'+encodeURIComponent(sportKey),{
   method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({portfolio_pct:value})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Portfolio unit update failed');
  btn.textContent='AUTO ON';
  await loadBasketballMonitors();
 }catch(e){btn.disabled=false;btn.textContent=original;alert(String(e.message||e))}
}

function monitorCard(x,sportKey){
 if(!x)return 'No data.';
 const win=x.win_pct===null||x.win_pct===undefined?'—':Number(x.win_pct).toFixed(1)+'%';
 const roi=x.roi_pct===null||x.roi_pct===undefined?'—':Number(x.roi_pct).toFixed(1)+'%';
 const feed=x.feed||{};
 const feedOk=feed.last_success_at&&!Number(feed.consecutive_errors||0);
 const unitValue=Number(x.unit_usdc||10).toFixed(2);
 const fixedValue=Number(x.fixed_unit_usdc||x.unit_usdc||10).toFixed(2);
 const pctValue=Number(x.portfolio_pct||10).toFixed(2);
 const autoPct=String(x.unit_mode||'fixed')==='portfolio_pct';
 const portfolioValue=x.portfolio_value_usdc===null||x.portfolio_value_usdc===undefined?null:Number(x.portfolio_value_usdc);
 const modeText=autoPct
  ? (x.unit_error?('AUTO '+pctValue+'% · '+monitorEsc(x.unit_error)):('AUTO '+pctValue+'% of $'+portfolioValue.toFixed(2)+' = 1u WIN $'+unitValue))
  : ('FIXED · 1u WIN $'+fixedValue);
 const fixedBtnStyle=autoPct?'opacity:.68':'font-weight:800;border-color:#86efac';
 const autoBtnStyle=autoPct?'font-weight:800;border-color:#86efac':'opacity:.68';
 const unitControl='<div class="capper-sizing">'
  +'<div class="capper-sizing-row"><span class="capper-sizing-label">Fixed 1u win $</span><div class="capper-sizing-controls"><button type="button" class="'+(!autoPct?'active':'')+'" aria-pressed="'+(!autoPct?'true':'false')+'" style="'+fixedBtnStyle+'" onclick="monitorSetUnitSize(\''+sportKey+'\',this)">SET 1U</button><input id="monitorUnitSize-'+sportKey+'" type="number" min="0.01" max="10000" step="0.01" value="'+fixedValue+'"></div></div>'
  +'<div class="capper-sizing-row"><span class="capper-sizing-label">Portfolio %</span><div class="capper-sizing-controls"><button type="button" class="'+(autoPct?'active':'')+'" aria-pressed="'+(autoPct?'true':'false')+'" style="'+autoBtnStyle+'" onclick="monitorSetPortfolioPct(\''+sportKey+'\',this)">AUTO %</button><input id="monitorPortfolioPct-'+sportKey+'" type="number" min="0.01" max="100" step="0.01" value="'+pctValue+'"></div></div>'
  +'<div class="capper-sizing-note">'+modeText+'<br><span>TO WIN sizing · risk changes with the live price</span></div>'
  +'</div>';
 const performance='<div class="capper-metrics-grid">'
  +'<div class="capper-metric"><span>Bets</span><b>'+Number(x.bets||0)+'</b></div>'
  +'<div class="capper-metric"><span>Open</span><b>'+Number(x.open||0)+'</b></div>'
  +'<div class="capper-metric"><span>W-L-P</span><b>'+Number(x.wins||0)+'-'+Number(x.losses||0)+'-'+Number(x.pushes||0)+'</b></div>'
  +'<div class="capper-metric"><span>Win</span><b>'+win+'</b></div>'
  +'<div class="capper-metric"><span>Stake</span><b>$'+Number(x.graded_stake_usdc||0).toFixed(2)+'</b></div>'
  +'<div class="capper-metric"><span>ROI</span><b>'+roi+'</b></div>'
  +'<div class="capper-metric"><span>Realized P/L</span><b class="capper-pnl '+monitorPnlClass(x.realized_pnl_usdc)+'">'+monitorMoney(x.realized_pnl_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>Live P/L</span><b class="capper-pnl '+monitorPnlClass(x.total_live_pnl_usdc)+'">'+monitorMoney(x.total_live_pnl_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>7D P/L</span><b class="'+monitorPnlClass(x.realized_pnl_7d_usdc)+'">'+monitorMoney(x.realized_pnl_7d_usdc)+'</b></div>'
  +'<div class="capper-metric"><span>30D P/L</span><b class="'+monitorPnlClass(x.realized_pnl_30d_usdc)+'">'+monitorMoney(x.realized_pnl_30d_usdc)+'</b></div>'
  +'</div>';
 return unitControl+performance+
  '<div class="monitor-feed '+(feedOk?'positive':'negative')+'">Feed '+(feedOk?'CONNECTED':'CHECK')+' · last success '+monitorTime(feed.last_success_at)+' · records '+Number(feed.last_poll_records||0)+(feed.last_record_error?' · '+monitorEsc(feed.last_record_error):'')+'</div>'+
  monitorPositions(x)+monitorSignals(x);
}

function monitorSyncPowerButton(sportKey,enabled){
 const btn=document.getElementById('monitorCapperPower-'+sportKey);
 if(!btn)return;
 const on=enabled!==false;
 btn.dataset.enabled=on?'1':'0';
 btn.classList.toggle('active',on);
 btn.classList.toggle('offline',!on);
 btn.setAttribute('aria-pressed',on?'true':'false');
 const text=btn.querySelector('.capper-power-text');
 if(text)text.textContent=on?'Online':'Offline';
}
async function monitorToggleCapper(sportKey,btn){
 const online=btn.dataset.enabled!=='0';
 if(online&&!confirm('Turn this basketball monitor OFFLINE? New automatic trades from it will pause.'))return;
 btn.disabled=true;
 try{
  const r=await fetch('/api/basketball-monitor/enabled/'+encodeURIComponent(sportKey),{
   method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!online})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Monitor status update failed');
  await loadBasketballMonitors();
 }catch(e){alert(String(e.message||e))}
 finally{btn.disabled=false}
}
function renderBasketballMonitors(){
 const w=document.getElementById('wnbaMonitorCard'),n=document.getElementById('nbaMonitorCard');
 if(w)w.innerHTML=monitorCard(basketballMonitorData['WNBA Monitor - WNBA'],'wnba');
 if(n)n.innerHTML=monitorCard(basketballMonitorData['NBA Monitor - NBA'],'nba');
 monitorSyncPowerButton('wnba',(basketballMonitorData['WNBA Monitor - WNBA']||{}).enabled);
 monitorSyncPowerButton('nba',(basketballMonitorData['NBA Monitor - NBA']||{}).enabled);
 capperSyncLast24hButtons();
}
window.addEventListener('capper-history-filter-change',renderBasketballMonitors);

async function loadBasketballMonitors(){
 try{
  const r=await fetch('/api/basketball-monitor/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Monitor status failed');
  basketballMonitorData=d.cappers||{};
  const wnba=basketballMonitorData['WNBA Monitor - WNBA']||{};
  const nba=basketballMonitorData['NBA Monitor - NBA']||{};
  const wnbaState=document.getElementById('wnbaMonitorState'),nbaState=document.getElementById('nbaMonitorState');
  if(wnbaState)wnbaState.textContent=(wnba.enabled!==false&&d.auto_trading&&d.live_trading)?'ENABLED · AUTO LIVE':'ENABLED · AUTO OFF';
  if(nbaState)nbaState.textContent=(nba.enabled!==false&&d.auto_trading&&d.live_trading)?'ENABLED · AUTO LIVE':'ENABLED · AUTO OFF';
  renderBasketballMonitors();
 }catch(e){
  const wnbaState=document.getElementById('wnbaMonitorState'),nbaState=document.getElementById('nbaMonitorState');
  if(wnbaState)wnbaState.textContent='Status unavailable';
  if(nbaState)nbaState.textContent='Status unavailable';
 }
}

async function monitorSell(tradeId,btn){
 if(!confirm('Sell the full tracked open position at the current executable market?'))return;
 const original=btn.textContent;btn.disabled=true;btn.textContent='SELLING…';
 try{
  const r=await fetch('/api/executor/request-sell/'+encodeURIComponent(tradeId),{method:'POST'}),q=await r.json();
  if(!r.ok)throw new Error(q.detail||'SELL request failed');
  const started=Date.now();
  while(Date.now()-started<90000){
   await new Promise(resolve=>setTimeout(resolve,1000));
   const sr=await fetch('/api/executor/request-status/'+encodeURIComponent(q.request_id),{cache:'no-store'}),sd=await sr.json();
   if(!sr.ok)throw new Error(sd.detail||'SELL status failed');
   if(sd.status==='DONE'){await loadBasketballMonitors();return}
   if(sd.status==='FAILED')throw new Error(sd.error||'SELL failed');
  }
  throw new Error('SELL timed out');
 }catch(e){btn.disabled=false;btn.textContent=original;alert(String(e.message||e))}
}
async function monitorSettle(tradeId,result,btn){
 if(!confirm('Manually settle this position as '+result+'?'))return;
 const original=btn.textContent;btn.disabled=true;btn.textContent='SETTLING…';
 try{
  const r=await fetch('/api/dashboard/manual-settle/'+encodeURIComponent(tradeId)+'/'+encodeURIComponent(result),{method:'POST'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual settlement failed');
  await loadBasketballMonitors();
 }catch(e){btn.disabled=false;btn.textContent=original;alert(String(e.message||e))}
}
loadBasketballMonitors();
setInterval(loadBasketballMonitors,10000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html
