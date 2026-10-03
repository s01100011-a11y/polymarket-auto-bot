(function(){
 let timingByFight={};
 let panelVisible=true;
 let installQueued=false;
 function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
 function localTime(iso){
  if(!iso)return '—';
  const d=new Date(iso);
  if(!Number.isFinite(d.getTime()))return '—';
  return new Intl.DateTimeFormat('en-MY',{timeZone:'Asia/Kuala_Lumpur',hour:'numeric',minute:'2-digit',hour12:true}).format(d)+' MYT';
 }
 function countdown(iso){
  const t=new Date(iso).getTime();
  if(!Number.isFinite(t))return '—';
  const ms=t-Date.now();
  if(ms<=-45*60*1000)return 'START TIME PASSED';
  if(ms<=0)return 'START WINDOW NOW';
  const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m '+String(sec).padStart(2,'0')+'s';
  return m+'m '+String(sec).padStart(2,'0')+'s';
 }
 function installRows(){
  installQueued=false;
  document.querySelectorAll('#ufcAutoTradingPanel .ufc-fight[data-fight-id]').forEach(row=>{
   const id=row.dataset.fightId||'';
   const t=timingByFight[id];
   let el=row.querySelector('.ufc-fight-time');
   if(!t){if(el)el.remove();return;}
   if(!el){
    el=document.createElement('div');
    el.className='ufc-fight-time';
    const matchup=row.querySelector('.ufc-matchup');
    if(matchup)matchup.insertAdjacentElement('afterend',el);else row.prepend(el);
   }
   const estimated=t.time_accuracy!=='official_block_start';
   const startAt=t.scheduled_start_at||'';
   const sig=[startAt,t.block||'',estimated?'1':'0'].join('|');
   if(el.dataset.sig!==sig){
    el.dataset.sig=sig;
    el.dataset.startAt=startAt;
    el.innerHTML='<span>'+esc(t.block||'UFC')+' · '+(estimated?'<span class="estimated">EST '+esc(localTime(startAt))+'</span>':'START '+esc(localTime(startAt)))+'</span><br><span class="countdown">'+esc(countdown(startAt))+'</span>';
   }
  });
 }
 function scheduleInstall(){
  if(installQueued)return;
  installQueued=true;
  requestAnimationFrame(installRows);
 }
 function tick(){
  if(!panelVisible||document.hidden)return;
  document.querySelectorAll('#ufcAutoTradingPanel .ufc-fight-time').forEach(el=>{
   const c=el.querySelector('.countdown');
   if(!c)return;
   const next=countdown(el.dataset.startAt||'');
   if(c.textContent!==next)c.textContent=next;
  });
 }
 async function loadTiming(){
  try{
   const r=await fetch('/api/dashboard/ufc-sh01-card',{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||r.status);
   const next={};
   for(const row of [...(d.main_card||[]),...(d.prelims||[])])if(row&&row.fight_id&&row.timing)next[row.fight_id]=row.timing;
   timingByFight=next;
   scheduleInstall();
  }catch(e){}
 }
 function mount(){
  const panel=document.getElementById('ufcAutoTradingPanel');
  const host=document.getElementById('ufcTomorrowCardV2')||document.getElementById('ufcTomorrowCard');
  if(host){
   const obs=new MutationObserver(()=>scheduleInstall());
   // Watch only top-level fight-card replacement. Do not observe subtree mutations
   // created by countdown text updates, which previously caused a recursive loop.
   obs.observe(host,{childList:true,subtree:false});
  }
  if(panel&&'IntersectionObserver' in window){
   const io=new IntersectionObserver(entries=>{panelVisible=!!entries[0]?.isIntersecting;},{rootMargin:'250px 0px'});
   io.observe(panel);
  }
  loadTiming();
  setInterval(tick,1000);
  setInterval(()=>{if(!document.hidden)loadTiming();},60000);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
