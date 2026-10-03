from __future__ import annotations

import time
import unicodedata
import re
from typing import Any

from fastapi import Depends, Query

from app import ufc_audit_data_v1 as audit
from app import ufc_live_activity_v1 as activity
from app import ufc_sh01_dashboard as ufc

live = activity.live
app = live.app
dashboard = live.dashboard

_CACHE_SECONDS = 5.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_FINISHED = {"won", "win", "lost", "loss", "push", "pushed", "void", "voided", "cancelled", "canceled", "settled"}


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _aliases(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").casefold())
    if not words:
        return set()
    out = {_compact(name)}
    last = words[-1]
    if len(last) >= 4:
        out.add(last)
    if len(words) >= 2:
        out.add(words[0][0] + last)
        out.add(words[0] + last)
    return {x for x in out if len(x) >= 4}


def _pick_text(row: dict[str, Any]) -> str:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    return " ".join(
        str(x or "")
        for x in (
            row.get("subject"),
            data.get("bet"),
            data.get("selection"),
            data.get("pick"),
        )
    )


def _matched_fighters(row: dict[str, Any], fighters: tuple[str, str]) -> list[str]:
    hay = _compact(_pick_text(row))
    matched: list[str] = []
    for fighter in fighters:
        if any(alias in hay for alias in _aliases(fighter)):
            matched.append(fighter)
    return matched


def _is_open(row: dict[str, Any]) -> bool:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    value = row.get("value") if isinstance(row.get("value"), dict) else {}
    status = str(value.get("result") or data.get("status") or "pending").strip().casefold()
    return status not in _FINISHED


def _normalize_pick(row: dict[str, Any], fighters: tuple[str, str]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    value = row.get("value") if isinstance(row.get("value"), dict) else {}
    return {
        "id": row.get("id"),
        "capper": data.get("capper") or row.get("source") or "Unknown capper",
        "bet": data.get("bet") or data.get("selection") or row.get("subject") or "UFC pick",
        "bet_type": data.get("bet_type") or row.get("market") or "",
        "book": data.get("book") or "",
        "units": value.get("units"),
        "odds_decimal": value.get("odds_decimal"),
        "odds_american": value.get("odds_american"),
        "wager_usd": value.get("wager_usd"),
        "to_win_usd": value.get("to_win_usd"),
        "status": value.get("result") or data.get("status") or "Pending",
        "bet_placed_date": data.get("bet_placed_date") or row.get("observed_at"),
        "matched_fighters": _matched_fighters(row, fighters),
        "source": row.get("source") or "Audit DB",
        "retrieved_at": row.get("retrieved_at"),
    }


def _payload(fight_id: str) -> dict[str, Any]:
    now = time.time()
    cached = _CACHE.get(fight_id)
    if cached and now - cached[0] < _CACHE_SECONDS:
        return dict(cached[1])

    fighters = ufc.FIGHTS.get(fight_id)
    if not fighters:
        result = {
            "ok": False,
            "fight_id": fight_id,
            "fighters": [],
            "capper_bets": [],
            "capper_count": 0,
            "bet_count": 0,
            "source": "Audit DB",
            "error": "Unknown UFC fight",
        }
        _CACHE[fight_id] = (now, result)
        return dict(result)

    try:
        rows = audit._audit_get(
            sport="UFC",
            data_type="capper_pick",
            event=audit._UFC_EVENT,
            limit=1000,
        )
        picks = [
            _normalize_pick(row, fighters)
            for row in rows
            if isinstance(row, dict)
            and _is_open(row)
            and _matched_fighters(row, fighters)
        ]
        picks.sort(
            key=lambda x: (
                str(x.get("capper") or "").casefold(),
                str(x.get("bet_placed_date") or ""),
                str(x.get("bet") or "").casefold(),
            )
        )
        cappers = sorted({str(x.get("capper") or "Unknown capper") for x in picks})
        result = {
            "ok": True,
            "fight_id": fight_id,
            "fighters": list(fighters),
            "fight": f"{fighters[0]} vs {fighters[1]}",
            "has_capper_bets": bool(picks),
            "capper_count": len(cappers),
            "bet_count": len(picks),
            "cappers": cappers,
            "capper_bets": picks,
            "source": "Audit DB",
            "error": None,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "fight_id": fight_id,
            "fighters": list(fighters),
            "fight": f"{fighters[0]} vs {fighters[1]}",
            "has_capper_bets": False,
            "capper_count": 0,
            "bet_count": 0,
            "cappers": [],
            "capper_bets": [],
            "source": "Audit DB",
            "error": f"{type(exc).__name__}: {exc}",
        }

    _CACHE[fight_id] = (now, result)
    return dict(result)


@app.get("/api/ufc-live/capper-bets", dependencies=[Depends(dashboard._auth)])
def ufc_live_capper_bets(fight_id: str = Query(..., min_length=1)) -> dict[str, Any]:
    return _payload(fight_id)


html = live.UFC_LIVE_HTML
if "ufc-current-capper-bets-v1" not in html:
    css = r'''
/* ufc-current-capper-bets-v1 */
.ufc-capper-window{margin:8px 6px 10px;border:2px outset #fff;background:#c0c0c0;color:#000}
.ufc-capper-title{background:#000080;color:#fff;padding:5px 7px;font-weight:900;display:flex;justify-content:space-between;gap:8px;align-items:center}
.ufc-capper-source{font-size:9px;font-weight:700;color:#fff}
.ufc-capper-body{padding:7px}
.ufc-capper-fight{font-weight:900;margin-bottom:6px;font-size:11px}
.ufc-capper-summary{border:2px inset #fff;background:#eee;padding:6px;font-size:10px;margin-bottom:5px}
.ufc-capper-summary.yes{color:#005a00;font-weight:900}.ufc-capper-summary.no{color:#555}.ufc-capper-summary.err{color:#a00000;font-weight:900}
.ufc-capper-list{display:grid;gap:5px}
.ufc-capper-bet{border:2px inset #fff;background:#eee;padding:6px;font-size:10px;line-height:1.35}
.ufc-capper-head{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:3px}
.ufc-capper-name{font-weight:900;color:#000080}.ufc-capper-type{font-weight:800}.ufc-capper-meta{color:#444}.ufc-capper-pick{font-weight:800;margin:2px 0}
.ufc-capper-empty{border:2px inset #fff;background:#eee;padding:7px;color:#555;font-size:10px}
'''
    html = html.replace("</style>", css + "</style>", 1)

    panel = r'''
<div class="ufc-capper-window" id="ufcCapperWindow">
  <div class="ufc-capper-title"><span>CURRENT FIGHT CAPPER BETS</span><span class="ufc-capper-source">AUDIT DB</span></div>
  <div class="ufc-capper-body">
    <div class="ufc-capper-fight" id="ufcCapperFight">Current fight: —</div>
    <div class="ufc-capper-summary no" id="ufcCapperSummary">Checking audit DB…</div>
    <div class="ufc-capper-list" id="ufcCapperList"></div>
  </div>
</div>
'''
    html = html.replace('<div id="fightHost"></div>', '<div id="fightHost"></div>' + panel, 1)

    js = r'''
<script id="ufc-current-capper-bets-v1">
(function(){
 let busy=false,timer=null,lastFight='';
 const nfmt=v=>{const n=Number(v);return Number.isFinite(n)?n:null};
 const odds=x=>{
  const d=nfmt(x.odds_decimal),a=nfmt(x.odds_american);
  if(d!==null&&a!==null)return d.toFixed(2)+' ('+(a>0?'+':'')+a.toFixed(0)+')';
  if(d!==null)return d.toFixed(2);
  if(a!==null)return (a>0?'+':'')+a.toFixed(0);
  return '—';
 };
 const units=v=>{const n=nfmt(v);return n===null?'—':(Math.round(n*1000)/1000).toString()+'u'};
 async function refresh(){
  if(busy||document.hidden){timer=setTimeout(refresh,5000);return}
  const f=typeof current==='function'?current():null;
  const fightId=String(f?.fight_id||'');
  const fightEl=document.getElementById('ufcCapperFight'),sum=document.getElementById('ufcCapperSummary'),list=document.getElementById('ufcCapperList');
  if(!fightId){
   if(fightEl)fightEl.textContent='Current fight: —';
   if(sum){sum.className='ufc-capper-summary no';sum.textContent='No current fight selected.'}
   if(list)list.innerHTML='';
   timer=setTimeout(refresh,2500);return;
  }
  busy=true;lastFight=fightId;
  try{
   const r=await fetch('/api/ufc-live/capper-bets?fight_id='+encodeURIComponent(fightId),{cache:'no-store'}),d=await r.json();
   if(fightId!==lastFight)return;
   if(fightEl)fightEl.textContent='Current fight: '+(d.fight||[f?.fighter_a,f?.fighter_b].filter(Boolean).join(' vs ')||fightId);
   if(!r.ok||!d.ok)throw Error(d.error||d.detail||'Audit DB request failed');
   const bets=Array.isArray(d.capper_bets)?d.capper_bets:[];
   if(sum){
    sum.className='ufc-capper-summary '+(bets.length?'yes':'no');
    sum.textContent=bets.length?(d.capper_count+' capper'+(d.capper_count===1?'':'s')+' · '+bets.length+' open bet'+(bets.length===1?'':'s')):'No capper bets found for this fight.';
   }
   if(list)list.innerHTML=bets.length?bets.map(x=>{
    const meta=[x.bet_type||'',x.book||'',odds(x),units(x.units)].filter(Boolean).join(' · ');
    const matched=(x.matched_fighters||[]).join(' / ');
    return '<div class="ufc-capper-bet"><div class="ufc-capper-head"><span class="ufc-capper-name">'+esc(x.capper||'Unknown capper')+'</span><span class="ufc-capper-type">'+esc(x.bet_type||'')+'</span></div><div class="ufc-capper-pick">'+esc(x.bet||'UFC pick')+'</div><div class="ufc-capper-meta">'+esc(meta)+(matched?' · '+esc(matched):'')+'</div></div>';
   }).join(''):'<div class="ufc-capper-empty">No audit DB capper action on this matchup.</div>';
  }catch(e){
   if(sum){sum.className='ufc-capper-summary err';sum.textContent='Audit DB unavailable: '+String(e.message||e)}
   if(list)list.innerHTML='';
  }finally{busy=false;timer=setTimeout(refresh,5000)}
 }
 window.ufcRefreshCapperBets=refresh;
 const oldChoose=window.choose;
 if(typeof oldChoose==='function')window.choose=function(id){const out=oldChoose(id);setTimeout(refresh,30);return out};
 const oldSetTab=window.setTab;
 if(typeof oldSetTab==='function')window.setTab=function(x){const out=oldSetTab(x);setTimeout(refresh,30);return out};
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
 setTimeout(refresh,120);
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = html
