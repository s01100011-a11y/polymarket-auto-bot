(function(){
 let panelVisible=true;
 let stopped=false;
 const lastText=new Map();
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
 function buttonMap(){
  const map=new Map();
  document.querySelectorAll('#ufcAutoTradingPanel button[data-fight][data-side]').forEach(btn=>map.set((btn.dataset.fight||'')+'|'+Number(btn.dataset.side||0),btn));
  return map;
 }
 async function poll(){
  if(stopped)return;
  const delay=(document.hidden||!panelVisible)?2000:500;
  if(document.hidden||!panelVisible){setTimeout(poll,delay);return;}
  const state=ensureState();
  try{
   const r=await fetch('/api/dashboard/ufc-stream-prices',{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||r.status);
   const connected=!!(d.stream||{}).connected;
   if(state){
    const text=connected?'PRICE STREAM LIVE · POLYMARKET TOP OF BOOK':'PRICE STREAM RECONNECTING';
    if(state.textContent!==text)state.textContent=text;
    state.classList.toggle('off',!connected);
   }
   const buttons=buttonMap();
   const updates=[];
   for(const row of d.rows||[]){
    const key=String(row.fight_id||'')+'|'+Number(row.side||0),btn=buttons.get(key);
    if(!btn)continue;
    const x=fmt(row);if(!x)continue;
    const fighter=btn.dataset.fighter||row.fighter||'';
    const text=fighter+' ML · '+x;
    const fresh=Number(row.age_ms||999999)<1500;
    const sig=text+'|'+(fresh?'1':'0')+'|'+String(row.age_ms==null?'':row.age_ms);
    if(lastText.get(key)===sig)continue;
    lastText.set(key,sig);
    updates.push([btn,text,fresh,row.age_ms]);
   }
   if(updates.length)requestAnimationFrame(()=>{
    for(const [btn,text,fresh,age] of updates){
     if(btn.textContent!==text)btn.textContent=text;
     btn.classList.toggle('stream-fresh',fresh);
     btn.title='Live top-of-book · '+(age==null?'age unavailable':age+'ms old');
    }
   });
  }catch(e){if(state){state.textContent='PRICE STREAM RECONNECTING';state.classList.add('off')}}
  setTimeout(poll,delay);
 }
 function mount(){
  const panel=document.getElementById('ufcAutoTradingPanel');
  if(panel&&'IntersectionObserver' in window){
   const io=new IntersectionObserver(entries=>{panelVisible=!!entries[0]?.isIntersecting;},{rootMargin:'300px 0px'});
   io.observe(panel);
  }
  ensureState();
  poll();
  window.addEventListener('pagehide',()=>{stopped=true;},{once:true});
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
