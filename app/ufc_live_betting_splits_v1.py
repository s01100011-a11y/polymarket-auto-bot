from __future__ import annotations

import hashlib
import html as html_lib
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, Query

from app import ufc_audit_data_v1 as audit
from app import ufc_live_page_v1 as live
from app import ufc_sh01_dashboard as ufc

app = live.app
dashboard = live.dashboard

_SOURCE_URL = os.getenv("UFC_BETTING_SPLITS_URL", "https://cleatz.com/public-betting/ufc/").strip()
_FETCH_SECONDS = max(15.0, float(os.getenv("UFC_BETTING_SPLITS_POLL_SECONDS", "60")))
_LOCK = threading.Lock()
_CACHE_AT = 0.0
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_ERROR: str | None = None
_PERSISTED: set[str] = set()
_BASE_CARD_PAYLOAD = ufc._card_payload

# Latest verified pre-fight public split snapshot available for the currently
# live Walker/Parkin bout. This is only used when the live public page cannot
# be parsed; the source and snapshot date remain visible in the UI.
_SNAPSHOT: dict[str, dict[str, Any]] = {
    "johnny-walker-vs-mick-parkin": {
        "available": True,
        "source": "CLEATZ sportsbook split snapshot",
        "observed_at": "2026-10-03",
        "fighters": {
            "Johnny Walker": {"bets_pct": "65%", "handle_pct": "56%", "odds": "-130"},
            "Mick Parkin": {"bets_pct": "35%", "handle_pct": "44%", "odds": "+110"},
        },
        "raw_data_type": "betting_splits",
        "fallback": True,
    }
}

_NAME_ALIASES: dict[str, list[str]] = {
    "Mick Parkin": ["Mick Parkin", "Michael Parkin"],
    "Khaos Williams": ["Khaos Williams", "Kalinn Williams"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aliases(name: str) -> list[str]:
    values = [name, *_NAME_ALIASES.get(name, [])]
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _plain_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script\b.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style\b.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def _pair_window(text: str, fighter_a: str, fighter_b: str) -> str | None:
    for a in _aliases(fighter_a):
        for b in _aliases(fighter_b):
            for left, right in ((a, b), (b, a)):
                m = re.search(re.escape(left) + r"\s+vs\.?\s+" + re.escape(right), text, flags=re.I)
                if m:
                    start = max(0, m.start() - 80)
                    return text[start : min(len(text), m.end() + 900)]
    # Some pages omit the matchup header but keep both fighter rows adjacent.
    lower = text.casefold()
    hits_a = [lower.find(alias.casefold()) for alias in _aliases(fighter_a)]
    hits_b = [lower.find(alias.casefold()) for alias in _aliases(fighter_b)]
    hits_a = [x for x in hits_a if x >= 0]
    hits_b = [x for x in hits_b if x >= 0]
    if not hits_a or not hits_b:
        return None
    a, b = min(hits_a), min(hits_b)
    if abs(a - b) > 700:
        return None
    start = max(0, min(a, b) - 80)
    return text[start : min(len(text), max(a, b) + 900)]


def _fighter_row(window: str, name: str) -> dict[str, Any] | None:
    for alias in _aliases(name):
        m = re.search(
            re.escape(alias)
            + r"(?P<between>.{0,110}?)\bBets\s*(?P<bets>\d{1,3})%\s*Handle\s*(?P<handle>\d{1,3})%",
            window,
            flags=re.I,
        )
        if not m:
            continue
        bets = int(m.group("bets"))
        handle = int(m.group("handle"))
        if not (0 <= bets <= 100 and 0 <= handle <= 100):
            continue
        between = m.group("between") or ""
        odds_match = re.search(r"(?<!\d)([+-]\d{2,4})(?!\d)", between)
        return {
            "bets_pct": f"{bets}%",
            "handle_pct": f"{handle}%",
            "odds": odds_match.group(1) if odds_match else None,
        }
    return None


def _extract_fight_split(text: str, fight_id: str, fighter_a: str, fighter_b: str) -> dict[str, Any] | None:
    window = _pair_window(text, fighter_a, fighter_b)
    if not window:
        return None
    a = _fighter_row(window, fighter_a)
    b = _fighter_row(window, fighter_b)
    if not a or not b:
        return None
    try:
        bets_sum = int(str(a["bets_pct"]).rstrip("%")) + int(str(b["bets_pct"]).rstrip("%"))
        handle_sum = int(str(a["handle_pct"]).rstrip("%")) + int(str(b["handle_pct"]).rstrip("%"))
    except Exception:
        return None
    if not (95 <= bets_sum <= 105 and 95 <= handle_sum <= 105):
        return None
    return {
        "available": True,
        "source": "CLEATZ UFC public betting",
        "observed_at": _now_iso(),
        "fighters": {fighter_a: a, fighter_b: b},
        "raw_data_type": "betting_splits",
        "fight_id": fight_id,
        "fallback": False,
    }


def _record_for_audit(fight_id: str, fighters: tuple[str, str], split: dict[str, Any]) -> dict[str, Any]:
    fighter_a, fighter_b = fighters
    values = split.get("fighters") if isinstance(split.get("fighters"), dict) else {}
    signature = "|".join(
        [
            fight_id,
            str(values.get(fighter_a)),
            str(values.get(fighter_b)),
            str(split.get("source") or ""),
        ]
    )
    rec_id = "ufc-split-" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
    return {
        "id": rec_id,
        "entity_key": f"ufc:{audit._UFC_EVENT_ID}:betting_splits:{fight_id}",
        "sport": "UFC",
        "league": "UFC",
        "event": audit._UFC_EVENT,
        "event_id": audit._UFC_EVENT_ID,
        "matchup": f"{fighter_a} vs {fighter_b}",
        "subject": f"{fighter_a} vs {fighter_b} moneyline betting splits",
        "data_type": "betting_splits",
        "market": "moneyline",
        "value": {"available": True},
        "data": {
            fighter_a: values.get(fighter_a),
            fighter_b: values.get(fighter_b),
            "fight_id": fight_id,
            "fallback": bool(split.get("fallback")),
        },
        "source": split.get("source") or "UFC public betting splits",
        "observed_at": split.get("observed_at") or _now_iso(),
        "retrieved_at": _now_iso(),
        "confidence": "verified" if not split.get("fallback") else "snapshot",
        "notes": "Moneyline public ticket and handle percentages for the UFC live dashboard.",
        "tags": ["ufc332", "betting-splits", "moneyline", fight_id],
    }


def _persist_best_effort(rows: dict[str, dict[str, Any]]) -> None:
    for fight_id, split in rows.items():
        fighters = ufc.FIGHTS.get(fight_id)
        if not fighters:
            continue
        record = _record_for_audit(fight_id, fighters, split)
        key = str(record.get("id") or "")
        if not key or key in _PERSISTED:
            continue
        try:
            audit._audit_post(record)
        except Exception:
            continue
        _PERSISTED.add(key)


def _fetch_live(*, force: bool = False) -> tuple[dict[str, dict[str, Any]], str | None]:
    global _CACHE_AT, _CACHE, _CACHE_ERROR
    now = time.monotonic()
    if not force and _CACHE_AT and now - _CACHE_AT < _FETCH_SECONDS:
        return dict(_CACHE), _CACHE_ERROR
    with _LOCK:
        now = time.monotonic()
        if not force and _CACHE_AT and now - _CACHE_AT < _FETCH_SECONDS:
            return dict(_CACHE), _CACHE_ERROR
        rows: dict[str, dict[str, Any]] = {}
        error: str | None = None
        try:
            with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                response = client.get(
                    _SOURCE_URL,
                    headers={
                        "User-Agent": "Mozilla/5.0 S01807 UFC splits/1.0",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                response.raise_for_status()
                text = _plain_text(response.text)
            for fight_id, fighters in ufc.FIGHTS.items():
                split = _extract_fight_split(text, fight_id, fighters[0], fighters[1])
                if split:
                    rows[fight_id] = split
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        # Keep verified Audit DB records preferred by the base payload. These
        # snapshots exist only so the live page never silently loses the split
        # when the public source blocks a request during the event.
        for fight_id, split in _SNAPSHOT.items():
            rows.setdefault(fight_id, dict(split))

        _CACHE = rows
        _CACHE_ERROR = error
        _CACHE_AT = time.monotonic()
        _persist_best_effort(rows)
        return dict(_CACHE), _CACHE_ERROR


def _card_payload_with_splits() -> dict[str, Any]:
    out = _BASE_CARD_PAYLOAD()
    live_rows, error = _fetch_live()
    for lane in ("main_card", "prelims"):
        card_rows = out.get(lane) if isinstance(out.get(lane), list) else []
        for row in card_rows:
            if not isinstance(row, dict):
                continue
            existing = row.get("betting_splits") if isinstance(row.get("betting_splits"), dict) else None
            if existing and existing.get("available"):
                continue
            split = live_rows.get(str(row.get("fight_id") or ""))
            if split:
                row["betting_splits"] = dict(split)
    out["betting_splits_source"] = "Audit DB + live public split feed"
    out["betting_splits_error"] = error
    return out


ufc._card_payload = _card_payload_with_splits


@app.get("/api/ufc-live/betting-splits", dependencies=[Depends(dashboard._auth)])
def ufc_live_betting_splits(fight_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    fighters = ufc.FIGHTS.get(fight_id)
    if not fighters:
        return {"ok": False, "fight_id": fight_id, "available": False, "error": "Unknown UFC fight"}

    # Prefer Audit DB if a newer canonical split has already been stored.
    try:
        canonical = audit._split_for_fight(audit._split_rows(), fighters[0], fighters[1])
    except Exception:
        canonical = None
    live_rows, error = _fetch_live()
    split = canonical if canonical and canonical.get("available") else live_rows.get(fight_id)
    return {
        "ok": True,
        "fight_id": fight_id,
        "fight": f"{fighters[0]} vs {fighters[1]}",
        "available": bool(split and split.get("available")),
        "betting_splits": split,
        "live_source_error": error,
        "updated_at": _now_iso(),
    }


def _install_inline_panel() -> None:
    page = live.UFC_LIVE_HTML
    marker = "ufc-live-betting-splits-v1"
    if marker in page:
        return

    css = r'''
/* ufc-live-betting-splits-v1 */
.ufc-split-live{border:2px inset #fff;background:#111;color:#eee;padding:7px;margin:0 0 7px;font-size:10px;line-height:1.45}
.ufc-split-live b{color:#fff}.ufc-split-line{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid #444;padding-top:4px;margin-top:4px}
.ufc-split-source{color:#aaa;font-size:9px;margin-top:4px}.ufc-split-sharp{color:#00a000;font-weight:900}.ufc-split-public{color:#8a5b00;font-weight:900}
'''
    page = page.replace("</style>", css + "</style>", 1)

    target = '<div class="ufc-capper-fight" id="ufcCapperFight">Current fight: —</div>'
    box = target + '<div class="ufc-split-live" id="ufcSplitLive"><b>BETTING SPLITS</b> · loading…</div>'
    if target in page:
        page = page.replace(target, box, 1)

    js = r'''
<script id="ufc-live-betting-splits-v1">
(function(){
 let splitBusy=false,splitFight='';
 function pctNum(v){const n=Number(String(v??'').replace('%',''));return Number.isFinite(n)?n:null}
 function renderSplit(d){
  const el=document.getElementById('ufcSplitLive');if(!el)return;
  const s=d?.betting_splits;
  if(!d?.available||!s?.fighters){el.innerHTML='<b>BETTING SPLITS</b> · no current split available';return}
  let html='<b>BETTING SPLITS</b>';
  for(const [name,x] of Object.entries(s.fighters)){
   const b=pctNum(x?.bets_pct),h=pctNum(x?.handle_pct),gap=(b!==null&&h!==null)?h-b:null;
   let cls='',tag='';
   if(gap!==null&&gap>=8){cls='ufc-split-sharp';tag=' · SHARP +'+gap.toFixed(0)+'pp'}
   else if(gap!==null&&gap<=-8){cls='ufc-split-public';tag=' · PUBLIC '+gap.toFixed(0)+'pp'}
   html+='<div class="ufc-split-line"><span>'+esc(name)+(x?.odds?' · '+esc(x.odds):'')+'</span><span class="'+cls+'">Bets '+esc(x?.bets_pct||'—')+' · Handle '+esc(x?.handle_pct||'—')+tag+'</span></div>';
  }
  html+='<div class="ufc-split-source">'+esc(s.source||'Audit DB')+(s.observed_at?' · '+esc(s.observed_at):'')+(s.fallback?' · SNAPSHOT':' · LIVE')+'</div>';
  el.innerHTML=html;
 }
 async function loadSplit(){
  if(splitBusy)return;
  let f=null;try{f=(typeof current==='function')?current():null}catch(_e){}
  const id=f?.fight_id;if(!id)return;
  splitBusy=true;splitFight=id;
  try{const r=await fetch('/api/ufc-live/betting-splits?fight_id='+encodeURIComponent(id),{cache:'no-store'}),d=await r.json();if(splitFight===id)renderSplit(d)}
  catch(e){const el=document.getElementById('ufcSplitLive');if(el)el.innerHTML='<b>BETTING SPLITS</b> · load error'}
  finally{splitBusy=false}
 }
 setInterval(loadSplit,5000);setTimeout(loadSplit,600);
})();
</script>
'''
    page = page.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = page


_install_inline_panel()
