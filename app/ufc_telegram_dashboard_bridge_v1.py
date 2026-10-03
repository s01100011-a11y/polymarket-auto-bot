from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import Depends

from app import ufc_audit_data_v1 as audit
from app import ufc_capper_bet_category_v3 as category
from app import ufc_current_capper_bets_v1 as current
from app import ufc_main_dashboard_cleanup_v1 as main_cleanup
from app import ufc_sh01_dashboard as ufc

app = current.app
dashboard = current.dashboard

_BRIDGE_URL = os.getenv(
    "UFC_CAPPER_BRIDGE_URL",
    os.getenv(
        "NFL_CAPPER_BRIDGE_URL",
        "https://telegram-chatgpt-bridge-production-286a.up.railway.app",
    ),
).strip().rstrip("/")
_SYNC_SECONDS = max(5.0, float(os.getenv("UFC_CAPPER_POLL_SECONDS", "15")))
_SYNC_LOCK = threading.Lock()
_SYNC_AT = 0.0
_SYNC_PICKS: list[dict[str, Any]] = []
_SYNC_ERROR: str | None = None
_PERSISTED: set[str] = set()
_BASE_PAYLOAD = current._payload

_TRACKED_SOURCE_KEYS = {"slam", "syndicate", "blacksmith"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _american_from_decimal(value: Any) -> int | None:
    try:
        price = Decimal(str(value))
    except Exception:
        return None
    if price <= 1:
        return None
    if price >= 2:
        american = (price - Decimal("1")) * Decimal("100")
    else:
        american = -(Decimal("100") / (price - Decimal("1")))
    return int(american.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _source_key(raw: dict[str, Any]) -> str:
    key = _compact(raw.get("source_key"))
    if key:
        return key
    source = _compact(raw.get("source"))
    if "syndicate" in source:
        return "syndicate"
    if "blacksmith" in source:
        return "blacksmith"
    if source.startswith("slam") or "slamallaccess" in source:
        return "slam"
    return source


def _fight_for_selection(selection: str) -> tuple[str, tuple[str, str], str] | None:
    probe = {"subject": selection, "data": {"selection": selection, "bet_type": ""}}
    matches: list[tuple[str, tuple[str, str], str]] = []
    for fight_id, fighters in ufc.FIGHTS.items():
        found = current._matched_fighters(probe, fighters)
        if len(found) == 1:
            matches.append((fight_id, fighters, found[0]))
    if len(matches) != 1:
        return None
    return matches[0]


def _clean_straight_ml(raw: dict[str, Any]) -> dict[str, Any] | None:
    if _source_key(raw) not in _TRACKED_SOURCE_KEYS:
        return None
    if str(raw.get("status") or "active").strip().casefold() not in {"active", "pending", "open"}:
        return None

    selection = str(raw.get("selection") or "").strip()
    # Known bridge OCR/parser typo from the UFC 332 Blacksmith card. Normalize
    # only the fighter name; the original Telegram record remains unchanged.
    selection = re.sub(r"\bMick\s+Parking\b", "Mick Parkin", selection, flags=re.I)
    if not selection:
        return None
    fight = _fight_for_selection(selection)
    if fight is None:
        return None
    fight_id, fighters, fighter = fight

    # Ignore the bridge's coarse bet_types label and classify the actual text.
    # This is important for entries such as "Green By Finish", which are not
    # straight moneylines even if a parser also tags them with "moneyline".
    probe = {
        "subject": selection,
        "market": "",
        "data": {"selection": selection, "bet": selection, "bet_type": ""},
    }
    inferred = category._category(probe, fighters)
    if inferred != "ML" or not current._is_straight_ml(probe, fighters):
        return None

    decimal_odds = raw.get("decimal_odds")
    american_odds = raw.get("american_odds")
    if american_odds in {None, ""}:
        american_odds = _american_from_decimal(decimal_odds)

    posted_at = str(raw.get("posted_at") or "").strip() or _now_iso()
    capper = str(raw.get("source") or raw.get("source_key") or "Telegram capper").strip()
    identity = "|".join(
        [
            _source_key(raw),
            str(raw.get("source_id") or ""),
            posted_at,
            _compact(selection),
            fight_id,
        ]
    )
    row_id = "ufc-tg-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    return {
        "id": row_id,
        "fight_id": fight_id,
        "fight": f"{fighters[0]} vs {fighters[1]}",
        "fighter": fighter,
        "capper": capper,
        "bet": selection,
        "bet_type": "Moneyline",
        "book": "",
        "units": raw.get("units"),
        "odds_decimal": decimal_odds,
        "odds_american": american_odds,
        "wager_usd": None,
        "to_win_usd": None,
        "status": "Pending",
        "bet_placed_date": posted_at,
        "matched_fighters": [fighter],
        "is_straight_ml": True,
        "source": "Telegram bridge",
        "source_key": _source_key(raw),
        "source_id": raw.get("source_id"),
        "retrieved_at": _now_iso(),
    }


def _audit_record(pick: dict[str, Any]) -> dict[str, Any]:
    # Prefix subject with capper so two cappers taking the same fighter do not
    # collapse onto one Audit DB entity key. Keep the exact posted selection in
    # data.bet/data.selection for display and fight matching.
    capper = str(pick.get("capper") or "Telegram capper")
    selection = str(pick.get("bet") or "UFC moneyline")
    return {
        "id": pick.get("id"),
        "sport": "UFC",
        "league": "UFC",
        "event": audit._UFC_EVENT,
        "event_id": audit._UFC_EVENT_ID,
        "matchup": pick.get("fight"),
        "subject": f"{capper} | {selection}",
        "data_type": "capper_pick",
        "market": "moneyline",
        "value": {
            "odds_american": pick.get("odds_american"),
            "odds_decimal": pick.get("odds_decimal"),
            "units": pick.get("units"),
            "result": "Pending",
        },
        "data": {
            "capper": capper,
            "selection": selection,
            "bet": selection,
            "bet_type": "Moneyline",
            "status": "Pending",
            "bet_placed_date": pick.get("bet_placed_date"),
            "fighter": pick.get("fighter"),
            "fight_id": pick.get("fight_id"),
            "telegram_source_id": pick.get("source_id"),
            "telegram_source_key": pick.get("source_key"),
        },
        "source": "Telegram bridge UFC poll",
        "observed_at": pick.get("bet_placed_date") or _now_iso(),
        "retrieved_at": _now_iso(),
        "confidence": "verified",
        "notes": "Straight UFC fighter moneyline normalized from the live Telegram sports feed.",
        "tags": ["ufc332", "telegram", "capper-pick", "moneyline", str(pick.get("source_key") or "")],
    }


def _persist_best_effort(picks: list[dict[str, Any]]) -> None:
    changed = False
    for pick in picks:
        key = str(pick.get("id") or "")
        if not key or key in _PERSISTED:
            continue
        try:
            audit._audit_post(_audit_record(pick))
        except Exception:
            # The dashboard still renders the direct Telegram result below. Keep
            # this key retryable so Audit DB catches up on a later poll.
            continue
        _PERSISTED.add(key)
        changed = True
    if changed:
        try:
            current._CACHE.clear()
        except Exception:
            pass


def _sync_bridge(*, force: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    global _SYNC_AT, _SYNC_PICKS, _SYNC_ERROR
    now = time.monotonic()
    if not force and _SYNC_AT and now - _SYNC_AT < _SYNC_SECONDS:
        return list(_SYNC_PICKS), _SYNC_ERROR

    with _SYNC_LOCK:
        now = time.monotonic()
        if not force and _SYNC_AT and now - _SYNC_AT < _SYNC_SECONDS:
            return list(_SYNC_PICKS), _SYNC_ERROR
        try:
            with httpx.Client(timeout=6.0) as client:
                response = client.get(
                    f"{_BRIDGE_URL}/public/ufc",
                    params={"minutes": 10080, "limit": 300, "include_graded": "false", "require_fresh": "true"},
                    headers={"Cache-Control": "no-cache"},
                )
                response.raise_for_status()
                body = response.json()
            raw_picks = body.get("picks") if isinstance(body, dict) else []
            picks = [
                parsed
                for raw in (raw_picks or [])
                if isinstance(raw, dict)
                for parsed in [_clean_straight_ml(raw)]
                if parsed is not None
            ]
            # One exact Telegram wager should appear once even if the bridge
            # returns duplicate historical rows.
            unique: dict[str, dict[str, Any]] = {}
            for pick in picks:
                unique[str(pick.get("id"))] = pick
            picks = sorted(
                unique.values(),
                key=lambda x: str(x.get("bet_placed_date") or ""),
                reverse=True,
            )
            _SYNC_PICKS = picks
            _SYNC_ERROR = None
            _SYNC_AT = time.monotonic()
            _persist_best_effort(picks)
        except Exception as exc:
            _SYNC_ERROR = f"{type(exc).__name__}: {exc}"
            _SYNC_AT = time.monotonic()
        return list(_SYNC_PICKS), _SYNC_ERROR


def _merge_payload(fight_id: str) -> dict[str, Any]:
    live_picks, bridge_error = _sync_bridge()
    base = _BASE_PAYLOAD(fight_id)
    direct = [pick for pick in live_picks if pick.get("fight_id") == fight_id]

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for pick in [*(base.get("capper_bets") or []), *direct]:
        if not isinstance(pick, dict):
            continue
        key = (
            _compact(pick.get("capper")),
            _compact(pick.get("bet")),
            str(pick.get("bet_placed_date") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(pick))

    merged.sort(
        key=lambda x: (
            str(x.get("bet_placed_date") or ""),
            str(x.get("capper") or "").casefold(),
        ),
        reverse=True,
    )
    cappers = sorted({str(x.get("capper") or "Unknown capper") for x in merged})
    out = dict(base)
    if direct and not out.get("ok"):
        out["ok"] = True
        out["error"] = None
    out.update(
        {
            "has_capper_bets": bool(merged),
            "capper_count": len(cappers),
            "bet_count": len(merged),
            "straight_ml_count": sum(1 for x in merged if x.get("is_straight_ml")),
            "cappers": cappers,
            "capper_bets": merged,
            "source": "Audit DB + Telegram bridge",
            "telegram_bridge_error": bridge_error,
        }
    )
    return out


# Existing /api/ufc-live/capper-bets resolves current._payload at request time,
# so replacing it here fixes the dedicated UFC page without duplicating routes.
current._payload = _merge_payload


@app.get("/api/dashboard/ufc-capper-bets", dependencies=[Depends(dashboard._auth)])
def dashboard_ufc_capper_bets() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for fight_id in ufc.FIGHTS:
        payload = current._payload(fight_id)
        error = payload.get("telegram_bridge_error") or payload.get("error")
        if error and str(error) not in errors:
            errors.append(str(error))
        for pick in payload.get("capper_bets") or []:
            if not isinstance(pick, dict) or not pick.get("is_straight_ml"):
                continue
            row = dict(pick)
            row["fight_id"] = fight_id
            row["fight"] = payload.get("fight")
            rows.append(row)

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            _compact(row.get("capper")),
            _compact(row.get("bet")),
            str(row.get("bet_placed_date") or ""),
        )
        deduped[key] = row
    rows = sorted(
        deduped.values(),
        key=lambda x: str(x.get("bet_placed_date") or ""),
        reverse=True,
    )
    return {
        "ok": bool(rows) or not errors,
        "event": audit._UFC_EVENT,
        "bets": rows,
        "bet_count": len(rows),
        "cappers": sorted({str(x.get("capper") or "Unknown capper") for x in rows}),
        "updated_at": _now_iso(),
        "errors": errors,
    }


def _install_main_dashboard_panel() -> None:
    html = dashboard.DASHBOARD_HTML
    marker = "ufc-telegram-dashboard-bridge-v1"
    if marker in html:
        return

    panel_at = html.find('id="ufcAutoTradingPanel"')
    if panel_at < 0:
        return
    grid = '  <div class="nfl-capper-grid"></div>\n'
    grid_at = html.find(grid, panel_at)
    if grid_at < 0:
        return

    extra = r'''
  <div class="ufc-capper-feed" id="ufcCapperFeedV1">
    <div class="ufc-capper-feed-head"><b>UFC CAPPER MONEYLINES</b><span id="ufcCapperFeedStamp">LOADING...</span></div>
    <div id="ufcCapperFeedRows" class="ufc-capper-feed-rows"><div class="ufc-capper-empty">Loading Syndicate / Blacksmith UFC bets...</div></div>
  </div>
'''
    insert_at = grid_at + len(grid)
    html = html[:insert_at] + extra + html[insert_at:]

    css = r'''
<style id="ufc-telegram-dashboard-bridge-v1-style">
#ufcCapperFeedV1{margin-top:8px;border:2px inset #fff;background:#c0c0c0;padding:5px;font:12px "MS Sans Serif",Tahoma,sans-serif}
.ufc-capper-feed-head{display:flex;justify-content:space-between;gap:8px;align-items:center;border-bottom:1px solid #808080;padding:2px 3px 5px;margin-bottom:4px}.ufc-capper-feed-head span{font-size:10px;color:#333}
.ufc-capper-feed-rows{display:grid;gap:3px}.ufc-capper-bet{display:grid;grid-template-columns:minmax(120px,1fr) minmax(120px,1.3fr) auto;gap:6px;align-items:center;background:#fff;border:1px solid #808080;padding:5px}.ufc-capper-bet .who{font-weight:700}.ufc-capper-bet .meta{text-align:right;white-space:nowrap}.ufc-capper-bet .fight{font-size:10px;color:#444}.ufc-capper-empty{background:#fff;border:1px solid #808080;padding:6px}
@media(max-width:760px){.ufc-capper-bet{grid-template-columns:1fr}.ufc-capper-bet .meta{text-align:left}}
</style>
'''
    html = html.replace("</head>", css + "</head>", 1)

    js = r'''
<script id="ufc-telegram-dashboard-bridge-v1">
(function(){
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const n=v=>{const x=Number(v);return Number.isFinite(x)?x:null};
 function odds(row){const d=n(row.odds_decimal);if(d!==null)return d.toFixed(2);const a=n(row.odds_american);return a===null?'—':(a>0?'+':'')+String(Math.round(a));}
 function units(row){const u=n(row.units);return u===null?'—':`${u}u`;}
 function stamp(v){try{return new Date(v).toLocaleString([], {month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'});}catch(_){return String(v||'');}}
 async function refresh(){
  const host=document.getElementById('ufcCapperFeedRows'),ts=document.getElementById('ufcCapperFeedStamp');if(!host)return;
  try{
   const r=await fetch('/api/dashboard/ufc-capper-bets',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);const d=await r.json();
   const rows=Array.isArray(d.bets)?d.bets:[];
   if(!rows.length){host.innerHTML='<div class="ufc-capper-empty">No open straight UFC moneyline capper bets.</div>';}
   else host.innerHTML=rows.map(x=>`<div class="ufc-capper-bet"><div><div class="who">${esc(x.capper)}</div><div class="fight">${esc(x.fight||'')}</div></div><div>${esc(x.bet||'UFC moneyline')}</div><div class="meta"><b>${esc(units(x))}</b> @ ${esc(odds(x))}<div class="fight">${esc(stamp(x.bet_placed_date))}</div></div></div>`).join('');
   if(ts)ts.textContent=`${rows.length} BET${rows.length===1?'':'S'} · ${stamp(d.updated_at)}`;
  }catch(e){host.innerHTML=`<div class="ufc-capper-empty">Capper feed error: ${esc(e.message||e)}</div>`;if(ts)ts.textContent='FEED ERROR';}
 }
 function start(){refresh();window.setInterval(refresh,15000);}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html


_install_main_dashboard_panel()
