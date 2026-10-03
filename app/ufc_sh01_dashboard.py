from __future__ import annotations

import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import Depends, HTTPException
from polymarket import PublicClient

from app import dashboard_live_control_v4 as live_control
from app import dashboard_sh01_capper_v3 as sh01_v3
from app import dashboard_sh01_capper_v4 as base
from app import nfl_capper_ingest as nfl
from app import termux_executor_dashboard as remote

app = base.app
dashboard = base.dashboard
core = base.core

EVENT_NAME = "UFC 332: Silva vs Wang"
EVENT_QUERY = "UFC 332"
EVENT_LOCAL_DATE = "2026-10-04"
CAPPER_LABEL = "SH01 - UFC"
DEFAULT_UNIT_USDC = Decimal(os.getenv("UFC_SH01_UNIT_USDC", "10"))

# This dashboard is intentionally scoped to the single UFC event requested for
# 2026-10-04 MYT. The fight list is explicit so a temporarily missing market
# never makes a bout disappear from the dashboard.
MAIN_CARD: tuple[tuple[str, str, str], ...] = (
    ("ateba-gautier-roman-kopylov", "Ateba Gautier", "Roman Kopylov"),
    ("roberto-soldic-khaos-williams", "Roberto Soldic", "Khaos Williams"),
    ("king-green-esteban-ribovics", "King Green", "Esteban Ribovics"),
    ("deiveson-figueiredo-payton-talbott", "Deiveson Figueiredo", "Payton Talbott"),
    ("natalia-silva-wang-cong", "Natalia Silva", "Wang Cong"),
)

PRELIMS: tuple[tuple[str, str, str], ...] = (
    ("imanol-rodriguez-alden-coria", "Imanol Rodriguez", "Alden Coria"),
    ("damian-pinas-andrey-pulyaev", "Damian Pinas", "Andrey Pulyaev"),
    ("marcus-mcghee-anthony-romero", "Marcus McGhee", "Anthony Romero"),
    ("anthony-wint-lucas-armand", "Anthony Wint", "Lucas Armand"),
    ("johnny-walker-mick-parkin", "Johnny Walker", "Mick Parkin"),
    ("rafael-dos-anjos-alexander-hernandez", "Rafael Dos Anjos", "Alexander Hernandez"),
    ("jacobe-smith-bruce-whitehead", "Jacobe Smith", "Bruce Whitehead"),
    ("marvin-vettori-ismail-naurdiev", "Marvin Vettori", "Ismail Naurdiev"),
    ("court-mcgee-eric-nolan", "Court McGee", "Eric Nolan"),
)

FIGHTS: dict[str, tuple[str, str]] = {
    fight_id: (fighter_a, fighter_b)
    for fight_id, fighter_a, fighter_b in MAIN_CARD + PRELIMS
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fighter_matches(label: Any, fighter: str) -> bool:
    return bool(label) and _compact(label) == _compact(fighter)


def _event_key(event: Any) -> str:
    return _text(getattr(event, "id", "") or getattr(event, "slug", "") or getattr(event, "title", ""))


def _event_has_fighters(event: Any, fighter_a: str, fighter_b: str) -> bool:
    title = _compact(getattr(event, "title", ""))
    if _compact(fighter_a) in title and _compact(fighter_b) in title:
        return True
    for market in getattr(event, "markets", ()) or ():
        labels = [label for label, obj in nfl._outcomes(market) if obj is not None]
        if any(_fighter_matches(label, fighter_a) for label in labels) and any(
            _fighter_matches(label, fighter_b) for label in labels
        ):
            return True
    return False


def _load_ufc_events() -> list[Any]:
    with PublicClient() as client:
        result = client.list_events(
            title_search=EVENT_QUERY,
            closed=False,
            page_size=100,
        ).first_page()
    events: dict[str, Any] = {}
    for event in result.items:
        slug = _text(getattr(event, "slug", ""))
        if slug and not slug.casefold().startswith("ufc-"):
            continue
        key = _event_key(event)
        if key:
            events[key] = event
    return list(events.values())


def _find_event(events: list[Any], fighter_a: str, fighter_b: str) -> Any | None:
    matches = [e for e in events if _event_has_fighters(e, fighter_a, fighter_b)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    # Prefer a direct fighter-v-fighter event title when Gamma returns both the
    # bout event and an umbrella/event container.
    direct = [
        e
        for e in matches
        if _compact(fighter_a) in _compact(getattr(e, "title", ""))
        and _compact(fighter_b) in _compact(getattr(e, "title", ""))
    ]
    return direct[0] if len(direct) == 1 else None


def _moneyline_market(event: Any, fighter_a: str, fighter_b: str) -> tuple[Any, dict[str, tuple[str, Any]]] | None:
    for market in getattr(event, "markets", ()) or ():
        outcomes = [(str(label), obj) for label, obj in nfl._outcomes(market) if obj is not None]
        if len(outcomes) != 2:
            continue
        mapping: dict[str, tuple[str, Any]] = {}
        for fighter in (fighter_a, fighter_b):
            hits = [(label, obj) for label, obj in outcomes if _fighter_matches(label, fighter)]
            if len(hits) == 1:
                mapping[fighter] = hits[0]
        if len(mapping) == 2:
            return market, mapping
    return None


def _price_fields(value: Any) -> dict[str, Any]:
    try:
        price = Decimal(str(value))
    except Exception:
        return {"price": None, "decimal_odds": None, "cents": None}
    if price <= 0 or price >= 1:
        return {"price": None, "decimal_odds": None, "cents": None}
    decimal_odds = (Decimal("1") / price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cents = (price * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return {
        "price": str(price),
        "decimal_odds": str(decimal_odds),
        "cents": str(cents),
    }


def _fight_payload(
    events: list[Any],
    fight_id: str,
    fighter_a: str,
    fighter_b: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fight_id": fight_id,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "market_found": False,
        "event_slug": None,
        "market": None,
        "fighters": [],
    }
    event = _find_event(events, fighter_a, fighter_b)
    market_match = _moneyline_market(event, fighter_a, fighter_b) if event is not None else None
    if event is None or market_match is None:
        row["fighters"] = [
            {"name": fighter_a, "side": 0, "available": False, **_price_fields(None)},
            {"name": fighter_b, "side": 1, "available": False, **_price_fields(None)},
        ]
        return row

    market, mapping = market_match
    event_slug = _text(getattr(event, "slug", ""))
    row["market_found"] = True
    row["event_slug"] = event_slug
    row["event_title"] = _text(getattr(event, "title", ""))
    row["market"] = _text(getattr(market, "question", "") or getattr(event, "title", ""))
    row["market_url"] = f"https://polymarket.com/sports/ufc/{event_slug}" if event_slug else None

    fighter_rows: list[dict[str, Any]] = []
    for side, fighter in enumerate((fighter_a, fighter_b)):
        label, outcome = mapping[fighter]
        asset_id = _text(getattr(outcome, "token_id", None) or getattr(outcome, "position_id", None))
        fighter_rows.append(
            {
                "name": fighter,
                "side": side,
                "outcome": label,
                "available": bool(asset_id),
                **_price_fields(getattr(outcome, "price", None)),
            }
        )
    row["fighters"] = fighter_rows
    return row


def _card_payload() -> dict[str, Any]:
    try:
        events = _load_ufc_events()
        load_error = None
    except Exception as exc:
        events = []
        load_error = f"{type(exc).__name__}: {exc}"

    main = [_fight_payload(events, *spec) for spec in MAIN_CARD]
    prelims = [_fight_payload(events, *spec) for spec in PRELIMS]
    unit_config = nfl._capper_unit_config(core, CAPPER_LABEL, DEFAULT_UNIT_USDC)
    all_rows = main + prelims
    market_count = sum(1 for row in all_rows if row.get("market_found"))
    return {
        "event": EVENT_NAME,
        "event_local_date": EVENT_LOCAL_DATE,
        "capper": "SH01",
        "sport": "UFC",
        "scope": "tomorrow_only",
        "updated_at": _now_iso(),
        "market_count": market_count,
        "fight_count": len(all_rows),
        "load_error": load_error,
        "unit": unit_config,
        "main_card": main,
        "prelims": prelims,
    }


def _live_quote(asset_id: str) -> dict[str, Decimal]:
    with PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        book = client.get_order_book(asset_id=asset_id)
    asks = getattr(book, "asks", None) or []
    best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    if best_ask is None or best_ask <= 0 or best_ask >= 1:
        raise RuntimeError("No executable UFC moneyline ask is available")
    if buy_price <= 0 or buy_price >= 1:
        raise RuntimeError(f"Invalid UFC BUY price {buy_price}")
    return {"buy_price": buy_price, "best_ask": best_ask, "spread": spread}


def _resolve_live_fighter(fight_id: str, side: int) -> dict[str, Any]:
    fighters = FIGHTS.get(fight_id)
    if fighters is None:
        raise HTTPException(status_code=404, detail="Unknown UFC 332 fight")
    if side not in {0, 1}:
        raise HTTPException(status_code=400, detail="Unknown fighter side")
    fighter_a, fighter_b = fighters
    fighter = fighters[side]
    try:
        events = _load_ufc_events()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not refresh UFC markets: {exc}") from exc
    event = _find_event(events, fighter_a, fighter_b)
    if event is None:
        raise HTTPException(status_code=409, detail="The selected UFC fight has no unique open Polymarket event")
    matched = _moneyline_market(event, fighter_a, fighter_b)
    if matched is None:
        raise HTTPException(status_code=409, detail="The selected UFC fight has no exact two-fighter moneyline market")
    market, mapping = matched
    label, outcome = mapping[fighter]
    asset_id = _text(getattr(outcome, "token_id", None) or getattr(outcome, "position_id", None))
    if not asset_id:
        raise HTTPException(status_code=409, detail="The selected fighter moneyline has no tradable token")
    event_slug = _text(getattr(event, "slug", ""))
    if not event_slug.casefold().startswith("ufc-"):
        raise HTTPException(status_code=409, detail="Resolved event is not a UFC Polymarket event")
    return {
        "fighter": fighter,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "outcome": label,
        "asset_id": asset_id,
        "event_slug": event_slug,
        "event_title": _text(getattr(event, "title", "")),
        "market": _text(getattr(market, "question", "") or getattr(event, "title", "")),
        "market_url": f"https://polymarket.com/sports/ufc/{event_slug}",
    }


def _existing_buy(strategy_pick_id: str) -> bool:
    try:
        remote._expire_stale_buys_persisted()
    except Exception:
        pass
    try:
        queue = remote._queue_load()
    except Exception:
        queue = {}
    for rec in queue.values():
        if rec.get("action") != "BUY" or str(rec.get("status") or "").upper() not in {
            "WAITING_APPROVAL",
            "PENDING",
            "LEASED",
        }:
            continue
        if str((rec.get("payload") or {}).get("strategy_pick_id") or "") == strategy_pick_id:
            return True
    try:
        executions = core._load(core.EXECUTIONS_FILE)
    except Exception:
        executions = {}
    for rec in executions.values():
        if str(rec.get("status") or "").upper() not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
            continue
        if str(rec.get("strategy_pick_id") or "") == strategy_pick_id:
            return True
    return False


@app.get("/api/dashboard/ufc-sh01-card", dependencies=[Depends(dashboard._auth)])
def ufc_sh01_card() -> dict[str, Any]:
    return _card_payload()


@app.post("/api/dashboard/ufc-sh01-buy/{fight_id}/{side}", dependencies=[Depends(dashboard._auth)])
def ufc_sh01_buy(fight_id: str, side: int) -> dict[str, Any]:
    if not core.bot_enabled():
        raise HTTPException(status_code=409, detail="Dashboard master switch is OFF")

    ready, executor_state = live_control._executor_ready()
    if not ready:
        if executor_state.get("geo_blocked"):
            raise HTTPException(status_code=409, detail="Termux executor is geoblocked")
        raise HTTPException(status_code=409, detail="Termux executor is offline")

    resolved = _resolve_live_fighter(fight_id, side)
    fighter = str(resolved["fighter"])
    strategy_pick_id = f"ufc332:{fight_id}:{_compact(fighter)}"
    if _existing_buy(strategy_pick_id):
        raise HTTPException(status_code=409, detail="This SH01 UFC fighter moneyline is already queued or open")

    try:
        quote = _live_quote(str(resolved["asset_id"]))
        unit_usdc = nfl._capper_unit_usdc(core, CAPPER_LABEL, DEFAULT_UNIT_USDC)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    target_profit = unit_usdc
    best_ask = quote["best_ask"]
    stake = nfl._stake_to_win_at_price(target_profit, best_ask)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise HTTPException(
            status_code=409,
            detail=(
                f"1u targets ${target_profit} profit and requires ${stake} stake at {best_ask}; "
                f"exceeds dashboard Auto trade cap ${core.MAX_AUTO_TRADE_USDC}"
            ),
        )

    used = core._daily_budget_used()
    pending = nfl._pending_auto_budget(remote)
    if used + pending + stake > core.MAX_DAILY_BUDGET_USDC:
        raise HTTPException(
            status_code=409,
            detail=(
                "Daily budget guard would be exceeded: "
                f"used=${used}, pending=${pending}, requested=${stake}, "
                f"limit=${core.MAX_DAILY_BUDGET_USDC}"
            ),
        )

    trade_id = f"ufc-sh01-{fight_id[:22]}-{uuid.uuid4().hex[:6]}"
    payload = {
        "market_url": resolved["market_url"],
        "outcome": resolved["outcome"],
        "market_type": "moneyline",
        "asset_id": resolved["asset_id"],
        "max_price": str(best_ask),
        "signal_buy_price": str(best_ask),
        "signal_spread": str(quote["spread"]),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "termux_executor",
        "auto": False,
        "manual": True,
        "strategy_execution_mode": "manual",
        "strategy_event_phase": "PREGAME",
        "strategy_source": "SH01",
        "strategy_sport": "UFC",
        "strategy_units": "1",
        "strategy_unit_usdc": str(unit_usdc),
        "strategy_target_profit_usdc": str(target_profit),
        "strategy_sizing_mode": "TO_WIN",
        "strategy_pick_id": strategy_pick_id,
        "strategy_posted_at": _now_iso(),
        "strategy_selection": f"{fighter} ML",
        "strategy_execution_selection": f"{fighter} ML",
        "strategy_telegram_source": "SH01",
        "event_title": resolved["event_title"],
        "market": resolved["market"],
    }
    queued = remote._enqueue("BUY", payload)
    status = str(queued.get("status") or "").upper()
    return {
        "ok": True,
        "status": status,
        "request_id": queued["id"],
        "trade_id": trade_id,
        "fighter": fighter,
        "fight": f"{resolved['fighter_a']} vs {resolved['fighter_b']}",
        "market_url": resolved["market_url"],
        "market": resolved["market"],
        "outcome": resolved["outcome"],
        "best_ask": str(best_ask),
        "decimal_odds": str((Decimal("1") / best_ask).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "units": "1",
        "unit_usdc": str(unit_usdc),
        "target_profit_usdc": str(target_profit),
        "stake_usdc": str(stake),
        "approval_required": status == "WAITING_APPROVAL",
        "approval_reason": payload.get("approval_reason"),
        "signal_decimal_odds": payload.get("signal_decimal_odds"),
        "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
    }


# Promote UFC to a first-class SH01 sport without rewriting the older dashboard
# module. Its payload functions read these globals at request time.
if "UFC" not in sh01_v3._KNOWN_SPORTS:
    sh01_v3._KNOWN_SPORTS = tuple(sh01_v3._KNOWN_SPORTS) + ("UFC",)

if not getattr(sh01_v3, "_ufc_sh01_sport_patch_v1", False):
    _original_record_sport = sh01_v3._record_sport

    def _record_sport_with_ufc(rec: dict[str, Any]) -> str | None:
        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        settlement = rec.get("settlement") if isinstance(rec.get("settlement"), dict) else {}
        direct = str(rec.get("strategy_sport") or "").strip().upper()
        if direct == "UFC":
            return "UFC"
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
        if any(needle in text for needle in ("/ufc/", "/event/ufc-", " ufc-", "ufc-")):
            return "UFC"
        return _original_record_sport(rec)

    sh01_v3._record_sport = _record_sport_with_ufc
    sh01_v3._ufc_sh01_sport_patch_v1 = True


html = dashboard.DASHBOARD_HTML
if "ufcSh01DashboardV1" not in html:
    css = r'''
/* ufcSh01DashboardV1 */
.ufc-card-wrap{margin-top:10px;border:2px inset #fff;background:#c0c0c0;padding:7px}.ufc-event-head{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:7px}.ufc-event-head b{font-size:13px}.ufc-event-meta{font-size:11px}.ufc-section{margin-top:8px}.ufc-section-title{background:#000080;color:#fff;padding:4px 6px;font-weight:900;font-size:11px}.ufc-fight{border:2px inset #fff;background:#111;color:#ddd;padding:7px;margin-top:5px}.ufc-matchup{font-weight:900;color:#fff;margin-bottom:6px}.ufc-ml-buttons{display:grid;grid-template-columns:1fr 1fr;gap:6px}.ufc-ml-buttons button{min-height:34px;font-weight:900;white-space:normal}.ufc-ml-buttons button:disabled{color:#777}.ufc-market-note{font-size:10px;color:#aaa;margin-top:5px}.ufc-state{font-weight:900}.ufc-state.ok{color:#006000}.ufc-state.warn{color:#800000}.ufc-action-msg{margin-top:7px;font-size:11px;font-weight:700}.ufc-action-msg.pending{color:#000080}.ufc-action-msg.approval{color:#800000}@media(max-width:600px){.ufc-ml-buttons{grid-template-columns:1fr}.ufc-fight{padding:6px}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    panel = r'''
<div class="nfl-capper-panel" id="ufcAutoTradingPanel">
  <div class="nfl-capper-head">
    <div class="capper-panel-title">UFC AUTO-TRADING</div>
  </div>
  <div class="capper-panel-status-row">
    <div class="capper-status-field"><div class="capper-status-label">STATUS</div><div class="nfl-capper-state" id="ufcSh01State">Loading…</div></div>
    <div class="capper-status-field"><div class="capper-status-label">CAPPER</div><div class="nfl-capper-state">SH01</div></div>
    <div class="capper-status-field"><div class="capper-status-label">EVENT</div><div class="nfl-capper-state">UFC 332</div></div>
  </div>
  <div class="nfl-capper-grid"></div>
  <div class="ufc-card-wrap">
    <div class="ufc-event-head"><b>UFC 332 · TOMORROW (MYT)</b><span class="ufc-event-meta" id="ufcSh01Unit">1U —</span></div>
    <div id="ufcTomorrowCard">Loading UFC 332 moneylines…</div>
    <div class="ufc-action-msg" id="ufcSh01Action"></div>
  </div>
</div>
'''

    js = r'''
<script id="ufcSh01DashboardV1">
(function(){
 let ufcCardData=null;
 function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
 function fmtOdds(f){
  if(!f||!f.available)return 'NO MARKET';
  const dec=Number(f.decimal_odds),c=Number(f.cents);
  if(Number.isFinite(dec)&&Number.isFinite(c))return dec.toFixed(2)+' ('+c.toFixed(c%1?1:0)+'¢)';
  return 'LIVE';
 }
 function fightHtml(f){
  const fighters=Array.isArray(f.fighters)?f.fighters:[];
  const buttons=fighters.map(x=>{
   const disabled=x.available?'':' disabled';
   return '<button type="button"'+disabled+' data-fight="'+esc(f.fight_id)+'" data-side="'+Number(x.side||0)+'" data-fighter="'+esc(x.name)+'" onclick="window.ufcSh01Buy(this)">'+esc(x.name)+' ML · '+fmtOdds(x)+'</button>';
  }).join('');
  const note=f.market_found?('Polymarket: '+esc(f.market||f.event_title||'moneyline')):'No exact open Polymarket two-fighter moneyline matched yet.';
  return '<div class="ufc-fight"><div class="ufc-matchup">'+esc(f.fighter_a)+' vs '+esc(f.fighter_b)+'</div><div class="ufc-ml-buttons">'+buttons+'</div><div class="ufc-market-note">'+note+'</div></div>';
 }
 function sectionHtml(title,rows){return '<div class="ufc-section"><div class="ufc-section-title">'+title+'</div>'+((rows||[]).map(fightHtml).join('')||'<div class="ufc-fight">No fights.</div>')+'</div>';}
 function render(data){
  ufcCardData=data||{};
  const state=document.getElementById('ufcSh01State'),unit=document.getElementById('ufcSh01Unit'),host=document.getElementById('ufcTomorrowCard');
  if(state){const n=Number(data.market_count||0),total=Number(data.fight_count||0);state.textContent=n+'/'+total+' moneylines matched';state.className='nfl-capper-state ufc-state '+(n===total?'ok':'warn');}
  if(unit){const u=(data.unit||{});unit.textContent='1U to win $'+Number(u.unit_usdc||0).toFixed(2)+(u.mode==='portfolio_pct'?' · '+Number(u.portfolio_pct||0).toFixed(2)+'% portfolio':' · fixed');}
  if(host){host.innerHTML=sectionHtml('MAIN CARD',data.main_card||[])+sectionHtml('PRELIMS',data.prelims||[])+(data.load_error?'<div class="ufc-market-note">Market refresh error: '+esc(data.load_error)+'</div>':'');}
 }
 async function load(){
  try{const r=await fetch('/api/dashboard/ufc-sh01-card',{cache:'no-store'}),d=await r.json();if(!r.ok)throw new Error(d.detail||'UFC card load failed');render(d)}catch(e){const s=document.getElementById('ufcSh01State');if(s){s.textContent='LOAD ERROR';s.className='nfl-capper-state ufc-state warn'}const h=document.getElementById('ufcTomorrowCard');if(h)h.textContent=String(e.message||e)}
 }
 window.ufcSh01Buy=async function(btn){
  const fight=btn.dataset.fight,side=Number(btn.dataset.side),fighter=btn.dataset.fighter;
  const unit=Number((((ufcCardData||{}).unit||{}).unit_usdc)||0);
  if(!confirm('Queue a REAL SH01 UFC moneyline on '+fighter+' to win 1U ($'+unit.toFixed(2)+') at the current executable Polymarket price?'))return;
  const msg=document.getElementById('ufcSh01Action'),old=btn.textContent;btn.disabled=true;btn.textContent='PLACING…';if(msg){msg.className='ufc-action-msg pending';msg.textContent='Refreshing '+fighter+' moneyline and preparing order…'}
  try{
   const r=await fetch('/api/dashboard/ufc-sh01-buy/'+encodeURIComponent(fight)+'/'+side,{method:'POST'}),d=await r.json();if(!r.ok)throw new Error(d.detail||'UFC BUY failed');
   const approval=String(d.status||'').toUpperCase()==='WAITING_APPROVAL';
   if(msg){msg.className='ufc-action-msg '+(approval?'approval':'pending');msg.textContent=approval?('Approval required · '+(d.approval_reason||'safeguard')+' · '+fighter+' '+Number(d.decimal_odds||0).toFixed(2)):('BUY queued · '+fighter+' '+Number(d.decimal_odds||0).toFixed(2)+' · stake $'+Number(d.stake_usdc||0).toFixed(2)+' to win 1U');}
   btn.textContent=approval?'APPROVAL REQUIRED':'QUEUED';
   setTimeout(()=>{load();if(typeof loadSh01Cappers==='function')loadSh01Cappers();},1200);
  }catch(e){btn.disabled=false;btn.textContent=old;if(msg){msg.className='ufc-action-msg approval';msg.textContent=String(e.message||e)}}
 };
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',load);else load();
 setInterval(load,30000);
})();
</script>
'''
    html = html.replace("</body>", panel + js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
