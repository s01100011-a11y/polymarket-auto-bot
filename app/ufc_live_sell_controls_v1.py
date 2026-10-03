from __future__ import annotations

from app import ufc_live_page_v1 as live


# Extend the isolated UFC live page without adding any new polling dependencies.
# SELL actions use the existing tracked Termux executor endpoint and trade IDs.
html = live.UFC_LIVE_HTML
if "ufc-live-sell-controls-v1" not in html:
    css = r'''
/* ufc-live-sell-controls-v1 */
.sell-row{display:flex;gap:6px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:6px}
.sell-btn{background:#c0c0c0!important;color:#000!important;border:2px outset #fff!important;padding:5px 9px!important;font-weight:900!important;min-height:30px}
.sell-btn:disabled{opacity:.55}
.sell-all{background:#800000!important;color:#fff!important}
.fight-total{border:1px solid #888;background:#080808;padding:7px;margin-top:8px;font-weight:900;line-height:1.45}
.fight-total .label{color:#fff}.fight-total .value.profit{color:#00ff66}.fight-total .value.loss{color:#ff7070}
.position .position-main{min-width:0;flex:1}
.position .position-sell{flex:0 0 auto}
@media(max-width:560px){.sell-row{align-items:stretch}.sell-row .sell-btn{width:100%}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
<script id="ufc-live-sell-controls-v1">
(function(){
 let fightOpen=[];
 let posBusy=false;
 let posTimer=null;
 const signedMoney=v=>{const n=Number(v||0);return (n>0?'+':'')+'$'+n.toFixed(2)};
 const pct=(a,b)=>{const x=Number(a||0),y=Number(b||0);return y>0?(x/y*100):0};
 const maxPayout=x=>{const s=Number(x?.shares||0);return Number.isFinite(s)&&s>0?s:0};
 const fightMatches=(x,f)=>{
  const names=[String(f?.fighter_a||'').toLowerCase(),String(f?.fighter_b||'').toLowerCase()].filter(Boolean);
  const text=String(x?.selection||x?.exact_position||x?.outcome||x?.market||'').toLowerCase();
  return names.some(n=>text.includes(n));
 };
 async function queueSell(tradeId,btn){
  if(!tradeId)return;
  const old=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='SELLING…'}
  try{
   const r=await fetch('/api/executor/request-sell/'+encodeURIComponent(tradeId),{method:'POST'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||d.error||'SELL failed');
   if(btn)btn.textContent=d.duplicate_prevented?'SELL QUEUED':'SELL QUEUED';
   setTimeout(()=>window.refreshPositions&&window.refreshPositions(),250);
  }catch(e){
   if(btn){btn.disabled=false;btn.textContent=old||'SELL POSITION'}
   alert(String(e.message||e));
  }
 }
 window.ufcLiveSell=function(tradeId,btn){return queueSell(tradeId,btn)};
 window.ufcLiveSellAll=async function(btn){
  const ids=fightOpen.map(x=>String(x.trade_id||x.id||'')).filter(Boolean);
  if(!ids.length)return;
  const old=btn.textContent;btn.disabled=true;btn.textContent='SELLING ALL…';
  try{
   const results=await Promise.allSettled(ids.map(id=>fetch('/api/executor/request-sell/'+encodeURIComponent(id),{method:'POST'}).then(async r=>{const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||'SELL failed');return d})));
   const ok=results.filter(x=>x.status==='fulfilled').length;
   const fail=results.length-ok;
   btn.textContent=fail?('QUEUED '+ok+' / FAILED '+fail):('SELL ALL QUEUED · '+ok);
   setTimeout(()=>window.refreshPositions&&window.refreshPositions(),250);
  }catch(e){btn.disabled=false;btn.textContent=old;alert(String(e.message||e))}
 };
 window.refreshPositions=async function(){
  const f=typeof current==='function'?current():null;
  if(!f){posTimer=setTimeout(window.refreshPositions,1000);return}
  if(posBusy)return;
  posBusy=true;
  if(posTimer){clearTimeout(posTimer);posTimer=null}
  try{
   const [a,b]=await Promise.all([
    fetch('/api/dashboard/sh01-cappers',{cache:'no-store'}),
    fetch('/api/ufc-live/pending',{cache:'no-store'})
   ]);
   const d=await a.json(),q=await b.json(),u=(d.sports||{}).UFC||{},all=Array.isArray(u.positions)?u.positions:[];
   const pv=document.getElementById('posValue');if(pv)pv.textContent=money(u.open_value_usdc||0);
   const pnl=Number(u.total_live_pnl_usdc||0),tp=document.getElementById('totalPnl');
   if(tp){tp.textContent=(pnl>0?'+':'')+money(pnl);tp.className=pnl>0?'profit':pnl<0?'loss':''}
   fightOpen=all.filter(x=>fightMatches(x,f)&&x.sell_available!==false&&!x.finished);
   const pending=(q.rows||[]).filter(x=>x.fight_id===f.fight_id);
   const h=document.getElementById('positions');if(!h)return;
   const fightPnl=fightOpen.reduce((s,x)=>s+Number(x.live_pnl_usdc||0),0);
   const maxTotal=fightOpen.reduce((s,x)=>s+maxPayout(x),0);
   const fightPct=pct(fightPnl,maxTotal);
   let out='';
   if(fightOpen.length){
    out+='<div class="fight-total"><div class="sell-row"><div><span class="label">TOTAL FIGHT P/L</span> · <span class="value '+(fightPnl>0?'profit':fightPnl<0?'loss':'')+'">'+signedMoney(fightPnl)+' / $'+maxTotal.toFixed(2)+' ('+fightPct.toFixed(0)+'%)</span></div><button type="button" class="sell-btn sell-all" onclick="ufcLiveSellAll(this)">SELL ALL · THIS FIGHT</button></div></div>';
   }
   for(const x of pending){
    out+='<div class="position pending"><b>'+esc(x.status)+' · '+esc(x.selection||'UFC BUY')+'</b><br>Stake '+money(x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.price)+' · Shares PENDING · P/L PENDING</div>';
   }
   for(const x of fightOpen){
    const p=Number(x.live_pnl_usdc||0),mx=maxPayout(x),pp=pct(p,mx),trade=String(x.trade_id||x.id||'');
    out+='<div class="position"><div class="sell-row"><div class="position-main"><b>OPEN · '+esc(x.exact_position||x.selection||x.outcome||'UFC position')+'</b><br>Stake '+money(x.open_cost_basis_usdc||x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.entry_price)+' · Shares '+(x.shares==null?'—':Number(x.shares).toFixed(2))+' · Value '+money(x.current_value_usdc)+'<br>P/L <span class="'+(p>0?'profit':p<0?'loss':'')+'">'+signedMoney(p)+' / $'+mx.toFixed(2)+' ('+pp.toFixed(0)+'%)</span></div>'+(trade?'<button type="button" class="sell-btn position-sell" onclick="ufcLiveSell(\''+esc(trade)+'\',this)">SELL POSITION</button>':'')+'</div></div>';
   }
   h.innerHTML=out||'<div class="meta">No pending/open positions for this fight.</div>';
  }catch(e){}finally{
   posBusy=false;
   posTimer=setTimeout(window.refreshPositions,1000);
  }
 };
 setTimeout(()=>window.refreshPositions(),50);
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = html
