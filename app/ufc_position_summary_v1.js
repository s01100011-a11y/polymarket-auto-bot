(function(){
 let busy=false;
 let lastUnit=null;
 function n(v){const x=Number(v);return Number.isFinite(x)?x:null}
 function money(v,signed){const x=n(v);if(x===null)return '—';return (signed&&x>0?'+':'')+'$'+x.toFixed(2)}
 function units(v){const x=n(v);if(x===null)return v?String(v)+'U':'—';return x.toFixed(2).replace(/\.00$/,'').replace(/(\.\d)0$/,'$1')+'U'}
 function oddsFromPrice(v){const p=n(v);if(p===null||p<=0||p>=1)return '—';const c=p*100;return (1/p).toFixed(2)+' ('+c.toFixed(Math.abs(c-Math.round(c))<0.05?0:1)+'¢)'}
 function cls(v){const x=n(v)||0;return x>0?'positive':x<0?'negative':'flat'}
 function norm(v){return String(v||'').toLowerCase().replace(/[^a-z0-9]+/g,'')}
 function nearPanel(){const p=document.getElementById('ufcAutoTradingPanel');if(!p)return false;const r=p.getBoundingClientRect();return r.bottom>-1200&&r.top<window.innerHeight+1200}
 function ensureSummary(){
  const p=document.getElementById('ufcAutoTradingPanel');if(!p)return null;
  let el=p.querySelector('.ufc-position-summary');
  if(!el){
   el=document.createElement('div');el.className='ufc-position-summary';
   const head=p.querySelector('.ufc-event-head');
   head?head.insertAdjacentElement('afterend',el):p.prepend(el);
  }
  return el;
 }
 function ensureFightUnit(fight){
  let el=fight.querySelector('.ufc-unit-value');
  if(!el){
   el=document.createElement('div');el.className='ufc-unit-value';
   const time=fight.querySelector('.ufc-fight-time'),match=fight.querySelector('.ufc-matchup');
   if(time)time.insertAdjacentElement('afterend',el);else if(match)match.insertAdjacentElement('afterend',el);else fight.prepend(el);
  }
  return el;
 }
 function ensureLiveHost(fight){
  let el=fight.querySelector('.ufc-live-positions');
  if(!el){
   el=document.createElement('div');el.className='ufc-live-positions';
   const inline=fight.querySelector('.ufc-inline-host');
   inline?inline.insertAdjacentElement('afterend',el):fight.appendChild(el);
  }
  return el;
 }
 function fighterAliases(fight){
  const out=[];
  fight.querySelectorAll('button[data-fighter]').forEach(b=>{
   const name=String(b.dataset.fighter||'').trim();if(!name)return;
   const last=name.split(/\s+/).pop()||'';
   out.push({full:norm(name),last:norm(last)});
  });
  return out;
 }
 function matchesFight(pos,fight){
  const hay=norm([pos.selection,pos.outcome,pos.exact_position,pos.market,pos.event_title].join(' '));
  if(!hay)return false;
  return fighterAliases(fight).some(a=>a.full&&hay.includes(a.full)||(a.last&&a.last.length>=4&&hay.includes(a.last)));
 }
 function renderPosition(pos){
  const bet=String(pos.exact_position||pos.selection||pos.outcome||'UFC position');
  const stake=pos.open_cost_basis_usdc??pos.stake_usdc;
  const pnl=pos.live_pnl_usdc??pos.pnl_usdc??pos.estimated_pnl;
  const shares=pos.shares==null||pos.shares===''?'—':Number(pos.shares).toFixed(2);
  return '<div class="ufc-live-position-row"><b>'+bet.replace(/[&<>]/g,'')+'</b> · '+units(pos.units)+' · Stake '+money(stake,false)+' · Odds '+oddsFromPrice(pos.entry_price)+' · Shares '+shares+' · P/L <span class="'+cls(pnl)+'">'+money(pnl,true)+'</span></div>';
 }
 function render(data,sizing){
  const sport=((data||{}).sports||{}).UFC||{};
  const unit=n((sizing||{}).unit_usdc);
  lastUnit=unit;
  const summary=ensureSummary();
  if(summary){
   const ov=sport.open_value_usdc,tp=sport.total_live_pnl_usdc;
   summary.innerHTML='<span>1U = '+money(unit,false)+'</span><span>POSITION VALUE '+money(ov,false)+'</span><span>LIVE P/L <b class="'+cls(tp)+'">'+money(tp,true)+'</b></span>';
  }
  const all=Array.isArray(sport.positions)?sport.positions:[];
  const open=all.filter(p=>p&&p.sell_available);
  document.querySelectorAll('#ufcAutoTradingPanel .ufc-fight').forEach(fight=>{
   const uv=ensureFightUnit(fight);if(uv)uv.textContent='UNIT VALUE · 1U = '+money(unit,false);
   const host=ensureLiveHost(fight);if(!host)return;
   const rows=open.filter(p=>matchesFight(p,fight));
   host.innerHTML=rows.map(renderPosition).join('');
  });
 }
 async function refresh(){
  if(busy||document.visibilityState!=='visible'||!nearPanel())return;
  busy=true;
  try{
   const [pr,sr]=await Promise.all([
    fetch('/api/dashboard/sh01-cappers',{cache:'no-store'}),
    fetch('/api/dashboard/ufc-sh01-sizing',{cache:'no-store'})
   ]);
   const [p,s]=await Promise.all([pr.json(),sr.json()]);
   if(pr.ok&&sr.ok)render(p,s);
  }catch(e){}finally{busy=false}
 }
 function loop(){refresh().finally(()=>setTimeout(loop,nearPanel()?2500:5000))}
 function mount(){ensureSummary();refresh();setTimeout(loop,2500)}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount,{once:true});else mount();
})();
