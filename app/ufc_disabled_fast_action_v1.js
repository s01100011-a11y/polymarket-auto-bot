(function(){
 let queued=false;
 function install(){
  queued=false;
  document.querySelectorAll('#ufcAutoTradingPanel .ufc-fight').forEach(fight=>{
   if(fight.querySelector('.ufc-disabled-fast-action'))return;
   const box=document.createElement('div');
   box.className='ufc-disabled-fast-action';
   const btn=document.createElement('button');
   btn.type='button';
   btn.disabled=true;
   btn.setAttribute('aria-disabled','true');
   btn.title='Visible layout control only. This button cannot place an order.';
   btn.textContent='ONE-TAP LIVE · DISABLED';
   box.appendChild(btn);
   const matchup=fight.querySelector('.ufc-matchup');
   if(matchup)matchup.insertAdjacentElement('afterend',box);else fight.prepend(box);
  });
 }
 function schedule(){if(queued)return;queued=true;requestAnimationFrame(install)}
 function mount(){
  const host=document.getElementById('ufcTomorrowCardV2')||document.getElementById('ufcTomorrowCard');
  if(host){const obs=new MutationObserver(schedule);obs.observe(host,{childList:true,subtree:false})}
  schedule();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
