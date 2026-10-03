(function(){
 let timingByFight={};
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
  let ms=t-Date.now();
  if(ms<=-45*60*1000)return 'START TIME PASSED';
  if(ms<=0)return 'START WINDOW NOW';
  const s=Math.floor(ms/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;
  if(h>0)return h+'h '+String(m).padStart(2,'0')+'m '+String(sec).padStart(2,'0')+'s';
  return m+'m '+String(sec).padStart(2,'0')+'s';
 }
 function installRows(){
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
   el.dataset.startAt=t.scheduled_start_at||'';
   el.innerHTML='<span>'+esc(t.block||'UFC')+' · '+(estimated?'<span class="estimated">EST '+esc(localTime(t.scheduled_start_at))+'</span>':'START '+esc(localTime(t.scheduled_start_at)))+'</span><br><span class="countdown">'+esc(countdown(t.scheduled_start_at))+'</span>';
  });
 }
 function tick(){
  document.querySelectorAll('#ufcAutoTradingPanel .ufc-fight-time').forEach(el=>{
   const c=el.querySelector('.countdown');
   if(c)c.textContent=countdown(el.dataset.startAt||'');
  });
 }
 async function loadTiming(){
  try{
   const r=await fetch('/api/dashboard/ufc-sh01-card',{cache:'no-store'}),d=await r.json();
   if(!r.ok)throw Error(d.detail||r.status);
   const next={};
   for(const row of [...(d.main_card||[]),...(d.prelims||[])])if(row&&row.fight_id&&row.timing)next[row.fight_id]=row.timing;
   timingByFight=next;
   installRows();
  }catch(e){}
 }
 const obs=new MutationObserver(()=>installRows());
 function mount(){
  const host=document.getElementById('ufcTomorrowCardV2')||document.getElementById('ufcTomorrowCard');
  if(host)obs.observe(host,{childList:true,subtree:true});
  loadTiming();
  setInterval(tick,1000);
  setInterval(loadTiming,60000);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
