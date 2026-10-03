(function(){
 function fmt(row){
  const o=Number(row.decimal_odds),a=Number(row.best_ask);
  if(!Number.isFinite(o)||!Number.isFinite(a))return null;
  const cents=a*100;
  return o.toFixed(2)+' ('+cents.toFixed(cents%1?1:0)+'¢)';
 }
 function ensureState(){
  const panel=document.getElementById('ufcAutoTradingPanel');if(!panel)return null;
  let el=panel.querySelector('.ufc-stream-state');
  if(!el){el=document.createElement('div');el.className='ufc-stream-state';el.textContent='PRICE STREAM CONNECTING…';const src=panel.querySelector('.ufc-v2-source');src?src.insertAdjacentElement('afterend',el):panel.prepend(el)}
  return el;
 }
 async function poll(){
  const state=ensureState();
  try{
   const r=await fetch('/api/dashboard/ufc-stream-prices',{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||r.status);
   const connected=!!(d.stream||{}).connected;
   if(state){state.textContent=connected?'PRICE STREAM LIVE · POLYMARKET TOP OF BOOK':'PRICE STREAM RECONNECTING';state.classList.toggle('off',!connected)}
   for(const row of d.rows||[]){
    const btn=document.querySelector('#ufcAutoTradingPanel button[data-fight="'+CSS.escape(String(row.fight_id||''))+'"][data-side="'+Number(row.side||0)+'"]');
    if(!btn)continue;
    const x=fmt(row);if(!x)continue;
    const fighter=btn.dataset.fighter||row.fighter||'';
    btn.textContent=fighter+' ML · '+x;
    btn.classList.toggle('stream-fresh',Number(row.age_ms||999999)<1500);
    btn.title='Live top-of-book · '+(row.age_ms==null?'age unavailable':row.age_ms+'ms old');
   }
  }catch(e){if(state){state.textContent='PRICE STREAM RECONNECTING';state.classList.add('off')}}
 }
 function mount(){ensureState();poll();setInterval(poll,350)}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
