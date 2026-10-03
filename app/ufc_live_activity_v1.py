from __future__ import annotations

from app import ufc_live_overview_v1 as overview

live = overview.live


# Add a lightweight UFC-only version of the main dashboard's Current watches &
# trades window. It reads the existing executor recent-actions feed and does not
# start any additional market/price/volume polling.
html = live.UFC_LIVE_HTML
if "ufc-live-activity-v1" not in html:
    css = r'''
/* ufc-live-activity-v1 */
.ufc-current-window{margin:10px 6px 8px;border:2px outset #fff;background:#c0c0c0;color:#000}
.ufc-current-title{background:#000080;color:#fff;padding:5px 7px;font-weight:900;display:flex;align-items:center;justify-content:space-between;gap:8px}
.ufc-current-title button{font:700 10px "MS Sans Serif",Tahoma,Arial,sans-serif;background:#c0c0c0;color:#000;border:2px outset #fff;padding:3px 7px}
.ufc-current-body{padding:7px}
.ufc-current-note{font-size:10px;margin:0 0 6px;color:#333}
.ufc-current-events{display:grid;gap:5px}
.ufc-current-event{display:grid;grid-template-columns:auto auto 1fr auto;gap:7px;align-items:start;border:2px inset #fff;background:#eee;padding:6px;font-size:10px;line-height:1.35}
.ufc-current-event .state{font-weight:900}.ufc-current-event.ok .state{color:#006000}.ufc-current-event.fail .state{color:#b00000}.ufc-current-event.run .state{color:#8a5b00}
.ufc-current-event .msg{overflow-wrap:anywhere}.ufc-current-event .stamp{white-space:nowrap;color:#555}
.ufc-current-empty{border:2px inset #fff;background:#eee;padding:8px;font-size:10px;color:#555}
@media(max-width:560px){.ufc-current-event{grid-template-columns:auto auto 1fr}.ufc-current-event .stamp{grid-column:1/-1}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    window_html = r'''
<div class="ufc-current-window" id="ufcCurrentWindow">
  <div class="ufc-current-title"><span>CURRENT WATCHES &amp; TRADES · UFC</span><button type="button" onclick="window.ufcRefreshCurrent&&window.ufcRefreshCurrent()">REFRESH</button></div>
  <div class="ufc-current-body">
    <div class="ufc-current-note">UFC BUY/SELL queue, execution confirmations and failures. Each row identifies the fight/selection when available.</div>
    <div class="ufc-current-events" id="ufcCurrentEvents"><div class="ufc-current-empty">Loading UFC executor activity…</div></div>
  </div>
</div>
'''
    html = html.replace('<div class="footer">', window_html + '<div class="footer">', 1)

    js = r'''
<script id="ufc-live-activity-v1">
(function(){
 let busy=false,timer=null;
 const stateClass=s=>s==='DONE'?'ok':s==='FAILED'?'fail':'run';
 const stateLabel=s=>s==='DONE'?'CONFIRMED':s==='FAILED'?'FAILED':s==='LEASED'?'EXECUTING':s==='WAITING_APPROVAL'?'WAITING APPROVAL':'QUEUED';
 const stamp=v=>{if(!v)return '—';try{return new Date(v).toLocaleString('en-MY',{timeZone:'Asia/Kuala_Lumpur',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true})+' MYT'}catch(e){return String(v)}};
 const isUfc=x=>String(x?.sport||'').toUpperCase()==='UFC'||String(x?.trade_id||'').startsWith('ufc-sh01-')||String(x?.order_label||x?.message||'').toUpperCase().includes('UFC');
 async function refresh(){
  if(busy||document.hidden){timer=setTimeout(refresh,3000);return}
  busy=true;
  try{
   const r=await fetch('/api/executor/recent-actions?limit=50',{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||'Status request failed');
   const events=(d.events||[]).filter(x=>['BUY','SELL'].includes(String(x.action||'').toUpperCase())&&isUfc(x)).slice(0,20);
   const box=document.getElementById('ufcCurrentEvents');if(!box)return;
   box.innerHTML=events.length?events.map(x=>{
    const label=x.order_label||x.message||[x.sport,x.selection].filter(Boolean).join(' · ')||'UFC order';
    const action=String(x.action||'').toUpperCase();
    return '<div class="ufc-current-event '+stateClass(String(x.status||'').toUpperCase())+'"><span><b>'+esc(action)+'</b></span><span class="state">'+esc(stateLabel(String(x.status||'').toUpperCase()))+'</span><span class="msg">'+esc(label)+'</span><span class="stamp">'+esc(stamp(x.updated_at||x.created_at))+'</span></div>';
   }).join(''):'<div class="ufc-current-empty">No recent UFC executor actions.</div>';
  }catch(e){const box=document.getElementById('ufcCurrentEvents');if(box)box.innerHTML='<div class="ufc-current-event fail"><span class="state">ERROR</span><span></span><span class="msg">'+esc(e.message||e)+'</span><span></span></div>'}
  finally{busy=false;timer=setTimeout(refresh,3000)}
 }
 window.ufcRefreshCurrent=refresh;
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
 setTimeout(refresh,100);
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = html
