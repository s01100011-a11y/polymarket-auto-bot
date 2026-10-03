from __future__ import annotations

import time
from typing import Any

from fastapi import Depends
from fastapi.responses import HTMLResponse

from app import dashboard_sh01_capper_v4 as sh01
from app import termux_executor_dashboard as remote
from app import ufc_price_stream_v1 as stream
from app import ufc_sh01_dashboard as ufc

app = ufc.app
dashboard = stream.dashboard
core = ufc.core


def _fight_id_from_payload(payload: dict[str, Any]) -> str | None:
    pick = str(payload.get("strategy_pick_id") or "")
    if pick.startswith("ufc332:"):
        parts = pick.split(":", 2)
        if len(parts) >= 2:
            return parts[1]
    trade = str(payload.get("trade_id") or "")
    if trade.startswith("ufc-sh01-"):
        body = trade[len("ufc-sh01-"):]
        return body.rsplit("-", 1)[0]
    return None


@app.get("/api/ufc-live/pending", dependencies=[Depends(dashboard._auth)])
def ufc_live_pending() -> dict[str, Any]:
    try:
        remote._expire_stale_buys_persisted()
    except Exception:
        pass
    try:
        queue = remote._queue_load()
    except Exception:
        queue = {}
    rows = []
    for rec in queue.values():
        if not isinstance(rec, dict) or str(rec.get("action") or "").upper() != "BUY":
            continue
        status = str(rec.get("status") or "").upper()
        if status not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if str(payload.get("strategy_sport") or "").upper() != "UFC":
            continue
        rows.append({
            "request_id": rec.get("id"),
            "fight_id": _fight_id_from_payload(payload),
            "status": status,
            "selection": payload.get("strategy_execution_selection") or payload.get("strategy_selection") or payload.get("outcome"),
            "stake_usdc": payload.get("budget_usdc"),
            "units": payload.get("strategy_units"),
            "unit_usdc": payload.get("strategy_unit_usdc"),
            "target_profit_usdc": payload.get("strategy_target_profit_usdc"),
            "price": payload.get("signal_buy_price") or payload.get("max_price"),
            "created_at": rec.get("created_at"),
        })
    rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"ok": True, "rows": rows}


@app.get("/api/ufc-live/executor", dependencies=[Depends(dashboard._auth)])
def ufc_live_executor() -> dict[str, Any]:
    state = remote._state()
    last_seen = float(state.get("last_seen_unix") or 0)
    paired = bool(state.get("token_hash"))
    connected = bool(paired and last_seen and time.time() - last_seen <= remote.CONNECTED_SECONDS)
    return {
        "ok": True,
        "connected": connected,
        "geo_blocked": bool(state.get("geo_blocked")),
        "worker_status": state.get("status"),
        "queue_poll_phase": state.get("queue_poll_phase"),
        "queue_poll_last_error": state.get("queue_poll_last_error"),
        "last_seen_at": state.get("last_seen_at"),
    }


UFC_LIVE_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>S01807 UFC Live</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#008080;color:#000;font-family:"MS Sans Serif",Tahoma,Arial,sans-serif;font-size:13px}.win{max-width:760px;margin:0 auto;min-height:100vh;background:#c0c0c0;border:2px outset #fff}.title{position:sticky;top:0;z-index:20;background:#000080;color:#fff;padding:7px;display:flex;gap:6px;align-items:center;font-weight:900}.title button,.btn{font:700 12px inherit;background:#c0c0c0;border:2px outset #fff;padding:6px 9px;color:#000}.title .name{flex:1}.status{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:6px}.box{background:#fff;border:2px inset #fff;padding:6px;min-width:0}.box b{display:block;font-size:11px}.ok{color:#006000}.bad{color:#b00000}.warn{color:#8a5b00}.controls{padding:6px;border-top:1px solid #808080;border-bottom:1px solid #808080}.row{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin:4px 0}.row input{width:84px;padding:4px}.active{background:#000080!important;color:#fff!important}.tabs{display:grid;grid-template-columns:1fr 1fr;gap:5px;padding:6px}.tabs button{padding:8px;font-weight:900}.fights{display:flex;gap:5px;overflow-x:auto;padding:0 6px 7px}.fights button{white-space:nowrap;padding:7px}.fight{margin:0 6px 8px;border:2px inset #fff;background:#111;color:#eee;padding:8px}.match{font-size:16px;font-weight:900;color:#fff}.time{margin-top:4px;font:700 11px monospace}.count{color:#00ff66}.est{color:#ffd34d}.meta{font-size:11px;color:#bbb;margin-top:5px}.splits{background:#1c1c1c;border:1px solid #555;padding:6px;margin:7px 0;font-size:11px}.prices{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px}.prices button{min-height:50px;font-size:13px;font-weight:900}.stream{font-size:11px;font-weight:900;margin:5px 0}.position{border:1px solid #777;background:#202020;padding:6px;margin-top:5px;line-height:1.45}.pending{color:#ffd34d}.profit{color:#00ff66}.loss{color:#ff7070}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:0 6px 6px}.disabled{opacity:.55}.footer{padding:6px;font-size:10px;color:#333}.volume{font-weight:900}.tiny{font-size:10px}.hidden{display:none!important}@media(max-width:560px){.status,.summary{grid-template-columns:1fr 1fr}.prices{grid-template-columns:1fr}.title{padding-top:max(7px,env(safe-area-inset-top))}}
</style></head><body><div class="win">
<div class="title"><button onclick="location.href='/'">← Dashboard</button><div class="name">🥊 UFC LIVE · S01807</div><button onclick="reloadCard()">Refresh card</button></div>
<div class="status"><div class="box"><b>EXECUTOR</b><span id="exec">…</span></div><div class="box"><b>PRICE STREAM</b><span id="stream">…</span></div><div class="box"><b>UNIT VALUE</b><span id="unitTop">1U —</span></div></div>
<div class="summary"><div class="box"><b>POSITION VALUE</b><span id="posValue">$0.00</span></div><div class="box"><b>LIVE P/L</b><span id="totalPnl">$0.00</span></div><div class="box"><b>ACTIVE FIGHT</b><span id="activeName">—</span></div></div>
<div class="controls"><div class="row"><button class="btn" data-size="fixed" onclick="setSizing('fixed')">SET 1U</button><input id="fixed" type="number" min="0.01" step="0.01"><span>Fixed $</span></div><div class="row"><button class="btn" data-size="portfolio_pct" onclick="setSizing('portfolio_pct')">AUTO %</button><input id="pct" type="number" min="0.01" max="100" step="0.01"><span>Portfolio %</span></div><div class="row"><button class="btn" data-size="performance" onclick="setSizing('performance')">AUTO PERF</button><span id="perf">Audit DB</span><button class="btn disabled" disabled title="Visible control only">ONE-TAP DISABLED</button></div><div class="tiny" id="sizeNote">Loading sizing…</div></div>
<div class="tabs"><button id="tabMain" onclick="setTab('main')">MAIN CARD</button><button id="tabPre" onclick="setTab('prelims')">PRELIMS</button></div><div class="fights" id="fightNav"></div><div id="fightHost"></div><div class="footer">Only the selected fight polls rapid live prices. Volume is cached and refreshed slowly. Card/splits/timing are Audit DB-backed. Execution uses the existing guarded UFC order flow.</div>
</div>
<script>
let card=null,tab='main',fightId=null,priceBusy=false,stopped=false,volumeCache=new Map(),unitValue=0;
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>{const n=Number(v);return Number.isFinite(n)?n:null},money=v=>{const n=num(v);return n===null?'—':'$'+n.toFixed(2)},odds=p=>{const n=num(p);return n&&n>0&&n<1?(1/n).toFixed(2)+' ('+(n*100).toFixed((n*100)%1?1:0)+'¢)':'—'};
function rows(){return tab==='main'?(card?.main_card||[]):(card?.prelims||[])}function current(){return rows().find(x=>x.fight_id===fightId)||rows()[0]||null}
function setTab(x){tab=x;fightId=(x==='main'?card?.main_card:card?.prelims)?.[0]?.fight_id||null;renderNav();renderFight();$('tabMain').classList.toggle('active',x==='main');$('tabPre').classList.toggle('active',x==='prelims')}
function choose(id){fightId=id;renderNav();renderFight();loadVolume(true);refreshPositions()}
function renderNav(){const h=$('fightNav');if(!h)return;h.innerHTML=rows().map(x=>'<button class="btn '+(x.fight_id===fightId?'active':'')+'" onclick="choose(\''+esc(x.fight_id)+'\')">'+esc(x.fighter_a.split(' ').slice(-1)[0])+' / '+esc(x.fighter_b.split(' ').slice(-1)[0])+'</button>').join('')}
function splitHtml(f){const s=f?.betting_splits;if(!s?.available)return '<div class="splits"><b>BETTING SPLITS</b> · Audit DB: no current split record</div>';let out='<div class="splits"><b>BETTING SPLITS</b>';for(const name of [f.fighter_a,f.fighter_b]){const x=s.fighters?.[name];if(x)out+='<br>'+esc(name)+(x.bets_pct?' · Bets '+esc(x.bets_pct):'')+(x.handle_pct?' · Handle '+esc(x.handle_pct):'')+(x.odds?' · '+esc(x.odds):'')}return out+'</div>'}
function timeHtml(f){const t=f?.timing;if(!t?.scheduled_start_at)return '<div class="time">START TIME —</div>';const d=new Date(t.scheduled_start_at),s=d.toLocaleString('en-MY',{timeZone:'Asia/Kuala_Lumpur',weekday:'short',hour:'2-digit',minute:'2-digit',hour12:true});return '<div class="time">START '+esc(s)+' MYT · <span class="count" data-start="'+esc(t.scheduled_start_at)+'"></span> '+(t.official?'':'<span class="est">EST</span>')+'</div>'}
function fighterButtons(f){return (f.fighters||[]).map(x=>'<button class="btn buy" '+(x.available?'':'disabled')+' data-side="'+Number(x.side||0)+'" data-fighter="'+esc(x.name)+'" onclick="buy(this)">'+esc(x.name)+' ML · <span class="px">'+(x.available?(Number(x.decimal_odds||0).toFixed(2)+' ('+Number(x.cents||0).toFixed(1)+'¢)'):'NO MARKET')+'</span></button>').join('')}
function renderFight(){const f=current(),h=$('fightHost');if(!f||!h){if(h)h.innerHTML='';return}$('activeName').textContent=f.fighter_a+' vs '+f.fighter_b;h.innerHTML='<div class="fight" data-fight="'+esc(f.fight_id)+'"><div class="match">'+esc(f.fighter_a)+' vs '+esc(f.fighter_b)+'</div>'+timeHtml(f)+'<div class="stream" id="fightStream">PRICE STREAM …</div>'+splitHtml(f)+'<div class="meta"><span class="volume" id="volume">VOL …</span> · '+esc(f.market||f.event_title||'Polymarket moneyline')+'</div><div class="meta">UNIT VALUE · 1U = '+money(unitValue)+'</div><div class="prices">'+fighterButtons(f)+'</div><div id="positions"></div></div>';tickCountdown();loadVolume(false)}
function tickCountdown(){document.querySelectorAll('[data-start]').forEach(el=>{const ms=new Date(el.dataset.start)-Date.now();if(ms<=0){el.textContent='STARTED / LIVE WINDOW';return}const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;el.textContent='IN '+h+'h '+String(m).padStart(2,'0')+'m '+String(ss).padStart(2,'0')+'s'})}
setInterval(tickCountdown,1000);
async function reloadCard(){try{const r=await fetch('/api/dashboard/ufc-sh01-card',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.detail||r.status);card=d;if(!fightId)fightId=(d.main_card||[])[0]?.fight_id||null;renderNav();renderFight();$('tabMain').classList.toggle('active',tab==='main');$('tabPre').classList.toggle('active',tab==='prelims')}catch(e){$('fightHost').innerHTML='<div class="fight loss">Card load error: '+esc(e.message||e)+'</div>'}}
async function loadSizing(){try{const r=await fetch('/api/dashboard/ufc-sh01-sizing',{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.detail||r.status);unitValue=Number(d.unit_usdc||0);$('unitTop').textContent='1U = '+money(unitValue);$('fixed').value=Number(d.fixed_unit_usdc||10).toFixed(2);$('pct').value=Number(d.portfolio_pct||10).toFixed(2);$('perf').textContent=(d.performance_pct!=null?Number(d.performance_pct).toFixed(2)+'% · ':'')+(d.tier||'Audit DB');document.querySelectorAll('[data-size]').forEach(b=>b.classList.toggle('active',b.dataset.size===d.mode));$('sizeNote').textContent=d.mode.toUpperCase()+' · 1U = '+money(unitValue);if(current())renderFight()}catch(e){$('sizeNote').textContent='Sizing error: '+String(e.message||e)}}
async function setSizing(mode){const value=mode==='fixed'?$('fixed').value:mode==='portfolio_pct'?$('pct').value:null;try{const r=await fetch('/api/dashboard/ufc-sh01-sizing',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,value})}),d=await r.json();if(!r.ok)throw Error(d.detail||r.status);await loadSizing()}catch(e){alert(String(e.message||e))}}
async function pollPrice(){if(stopped)return;const f=current();if(!f||document.hidden||priceBusy){setTimeout(pollPrice,300);return}priceBusy=true;try{const r=await fetch('/api/dashboard/ufc-stream-prices?fight_id='+encodeURIComponent(f.fight_id),{cache:'no-store'}),d=await r.json();if(!r.ok)throw Error(d.detail||r.status);const state=$('stream'),fs=$('fightStream');const live=!!d.stream?.connected;(state&&(state.textContent=live?'LIVE':'RECONNECTING',state.className=live?'ok':'warn'));if(fs){fs.textContent=live?'PRICE STREAM LIVE · POLYMARKET TOP OF BOOK':'PRICE STREAM RECONNECTING';fs.className='stream '+(live?'ok':'warn')}for(const row of d.rows||[]){const b=document.querySelector('.buy[data-side="'+Number(row.side||0)+'"]');if(!b)continue;const px=b.querySelector('.px');if(px&&row.best_ask)px.textContent=Number(row.decimal_odds||0).toFixed(2)+' ('+(Number(row.best_ask)*100).toFixed(1)+'¢)';b.title='Top of book · '+(row.age_ms==null?'age —':row.age_ms+'ms old')}}catch(e){}finally{priceBusy=false;setTimeout(pollPrice,300)}}
async function loadVolume(force){const f=current();if(!f?.market_url)return;const key=f.fight_id,old=volumeCache.get(key);if(!force&&old&&Date.now()-old.ts<90000){const el=$('volume');if(el)el.textContent=old.text;return}try{const p=new URLSearchParams({market_url:f.market_url,market_hint:f.market||'',sport:'UFC'}),r=await fetch('/api/dashboard/market-volume?'+p,{cache:'no-store'}),d=await r.json();let text=d.available?'VOL '+money(d.volume_usdc)+(d.volume_24h_usdc!=null?' · 24H '+money(d.volume_24h_usdc):''):'VOL —';volumeCache.set(key,{ts:Date.now(),text});const el=$('volume');if(el)el.textContent=text}catch(e){const el=$('volume');if(el)el.textContent='VOL —'}}
async function refreshPositions(){const f=current();if(!f)return;try{const [a,b]=await Promise.all([fetch('/api/dashboard/sh01-cappers',{cache:'no-store'}),fetch('/api/ufc-live/pending',{cache:'no-store'})]);const d=await a.json(),q=await b.json(),u=(d.sports||{}).UFC||{},all=Array.isArray(u.positions)?u.positions:[];$('posValue').textContent=money(u.open_value_usdc||0);const pnl=Number(u.total_live_pnl_usdc||0);$('totalPnl').textContent=(pnl>0?'+':'')+money(pnl);$('totalPnl').className=pnl>0?'profit':pnl<0?'loss':'';const names=[f.fighter_a.toLowerCase(),f.fighter_b.toLowerCase()];const open=all.filter(x=>names.some(n=>String(x.selection||x.exact_position||x.outcome||'').toLowerCase().includes(n)));const pending=(q.rows||[]).filter(x=>x.fight_id===f.fight_id);const h=$('positions');if(!h)return;let html='';for(const x of pending){html+='<div class="position pending"><b>'+esc(x.status)+' · '+esc(x.selection||'UFC BUY')+'</b><br>Stake '+money(x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.price)+' · Shares PENDING · P/L PENDING</div>'}for(const x of open){const p=Number(x.live_pnl_usdc||0);html+='<div class="position"><b>OPEN · '+esc(x.exact_position||x.selection||x.outcome||'UFC position')+'</b><br>Stake '+money(x.open_cost_basis_usdc||x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.entry_price)+' · Shares '+(x.shares==null?'—':Number(x.shares).toFixed(2))+' · Value '+money(x.current_value_usdc)+' · P/L <span class="'+(p>0?'profit':p<0?'loss':'')+'">'+(p>0?'+':'')+money(p)+'</span></div>'}h.innerHTML=html||'<div class="meta">No pending/open positions for this fight.</div>'}catch(e){}setTimeout(refreshPositions,1000)}
async function pollExecutor(){try{const r=await fetch('/api/ufc-live/executor',{cache:'no-store'}),d=await r.json(),el=$('exec');if(d.geo_blocked){el.textContent='GEOBLOCKED';el.className='bad'}else if(d.connected){el.textContent='ONLINE';el.className='ok'}else{el.textContent='OFFLINE';el.className='bad'}}catch(e){}setTimeout(pollExecutor,2000)}
async function buy(btn){const f=current(),fighter=btn.dataset.fighter,side=Number(btn.dataset.side);if(!f)return;if(!confirm('Queue a REAL SH01 UFC moneyline on '+fighter+' to win 1U ('+money(unitValue)+') at the current executable Polymarket price?'))return;const old=btn.innerHTML;btn.disabled=true;btn.textContent='PLACING…';try{const r=await fetch('/api/dashboard/ufc-sh01-buy/'+encodeURIComponent(f.fight_id)+'/'+side,{method:'POST'}),d=await r.json();if(!r.ok)throw Error(d.detail||'BUY failed');btn.textContent=String(d.status||'QUEUED');refreshPositions();setTimeout(()=>{btn.disabled=false;btn.innerHTML=old},1200)}catch(e){btn.disabled=false;btn.innerHTML=old;alert(String(e.message||e))}}
window.addEventListener('pagehide',()=>stopped=true,{once:true});document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadVolume(false)});loadSizing();reloadCard().then(()=>{setTab('main');loadVolume(false)});pollPrice();refreshPositions();pollExecutor();setInterval(()=>loadVolume(false),90000);
</script></body></html>'''


@app.get("/ufc-live", response_class=HTMLResponse, dependencies=[Depends(dashboard._auth)])
def ufc_live_page() -> HTMLResponse:
    return HTMLResponse(UFC_LIVE_HTML, headers={"Cache-Control": "no-store"})


# Redirect the UFC taskbar entry to the isolated page. Capture at document level
# so the older scroll-to-panel handler never fires first.
html = dashboard.DASHBOARD_HTML
if "ufc-live-page-link-v1" not in html:
    js = r'''<script id="ufc-live-page-link-v1">document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('button[data-jump="ufc"]');if(!b)return;e.preventDefault();e.stopImmediatePropagation();window.location.href='/ufc-live';},true);</script>'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
