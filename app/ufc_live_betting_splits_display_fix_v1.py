from __future__ import annotations

from app import ufc_live_betting_splits_v1 as splits

live = splits.live
html = live.UFC_LIVE_HTML
marker = "ufc-splits-main-row-fix-v1"
if marker not in html:
    patch = r'''
<style id="ufc-splits-main-row-fix-v1">#ufcSplitLive{display:none!important}.fight .splits .sl{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid #444;padding-top:4px;margin-top:4px}.fight .splits .ss{color:#aaa;font-size:9px;margin-top:4px}.fight .splits .sharp{color:#00d060;font-weight:900}.fight .splits .public{color:#ffd34d;font-weight:900}</style>
<script>
(function(){
 let busy=false,t=null,last='';
 const pct=v=>{const n=Number(String(v??'').replace('%',''));return Number.isFinite(n)?n:null};
 function show(d){const e=document.querySelector('#fightHost .fight .splits');if(!e)return;const s=d?.betting_splits;if(!d?.available||!s?.fighters){e.innerHTML='<b>BETTING SPLITS</b> · no current split available';return}const f=typeof current==='function'?current():null;let h='<b>BETTING SPLITS</b>';for(const n of [f?.fighter_a,f?.fighter_b].filter(Boolean)){const x=s.fighters?.[n];if(!x)continue;const b=pct(x.bets_pct),q=pct(x.handle_pct),g=b!==null&&q!==null?q-b:null;let c='',tag='';if(g!==null&&g>=8){c='sharp';tag=' · SHARP +'+g.toFixed(0)+'pp'}else if(g!==null&&g<=-8){c='public';tag=' · PUBLIC '+g.toFixed(0)+'pp'}h+='<div class="sl"><span>'+esc(n)+(x.odds?' · '+esc(x.odds):'')+'</span><span class="'+c+'">Bets '+esc(x.bets_pct||'—')+' · Handle '+esc(x.handle_pct||'—')+tag+'</span></div>'}h+='<div class="ss">'+esc(s.source||'Audit DB')+(s.observed_at?' · '+esc(s.observed_at):'')+(s.fallback?' · SNAPSHOT':' · LIVE')+'</div>';e.innerHTML=h}
 async function run(){clearTimeout(t);if(busy||document.hidden){t=setTimeout(run,5000);return}const f=typeof current==='function'?current():null,id=String(f?.fight_id||'');if(!id){t=setTimeout(run,2500);return}busy=true;last=id;try{const r=await fetch('/api/ufc-live/betting-splits?fight_id='+encodeURIComponent(id),{cache:'no-store'}),d=await r.json();if(r.ok&&id===last)show(d)}catch(_e){}finally{busy=false;t=setTimeout(run,5000)}}
 const oc=window.choose;if(typeof oc==='function')window.choose=function(id){const x=oc(id);setTimeout(run,50);return x};const ot=window.setTab;if(typeof ot==='function')window.setTab=function(x){const y=ot(x);setTimeout(run,50);return y};setTimeout(run,150);
})();
</script>
'''
    html = html.replace('</body>', patch + '</body>', 1)
    live.UFC_LIVE_HTML = html
