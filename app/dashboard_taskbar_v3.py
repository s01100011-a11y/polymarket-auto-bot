from __future__ import annotations

from app import dashboard_taskbar_v2 as base

dashboard = base.dashboard

html = dashboard.DASHBOARD_HTML
# The main Current watches & trades window is global. Pull the full recent
# executor window so a burst of UFC activity cannot hide NFL/CFB/NBA/WNBA/etc.
html = html.replace("/api/executor/recent-actions?limit=12", "/api/executor/recent-actions?limit=50")
html = html.replace(
    "Recent BUY/SELL confirmations and failures",
    "Recent BUY/SELL confirmations and failures across all sports",
    1,
)
if "s01807TaskbarV3" not in html:
    css = r'''
/* s01807TaskbarV3 */
.portfolio-live-source,.portfolio-live-sport{color:#fff;font-weight:700}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
<script id="s01807TaskbarV3">
(function(){
 const bar=document.getElementById('s01807TaskbarV1');
 if(!bar)return;

 function jumpOtherToCurrent(){
  const currentTab=document.querySelector('[data-tab="current"]');
  if(currentTab&&typeof currentTab.click==='function')currentTab.click();
  const current=document.getElementById('current');
  if(!current)return;
  bar.querySelectorAll('button[data-jump]').forEach(b=>b.classList.toggle('active',b.dataset.jump==='other'));
  requestAnimationFrame(()=>{
   const y=Math.max(0,current.getBoundingClientRect().top+window.scrollY-bar.offsetHeight-10);
   window.scrollTo({top:y,behavior:'smooth'});
  });
 }

 /* Override the older Other -> Trade history behavior. */
 bar.addEventListener('click',function(e){
  const b=e.target.closest('button[data-jump="other"]');
  if(!b)return;
  e.preventDefault();
  e.stopImmediatePropagation();
  jumpOtherToCurrent();
 },true);
 const otherBtn=bar.querySelector('button[data-jump="other"]');
 if(otherBtn)otherBtn.title='Other / Current watches & trades';

 function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
 function money(v){const n=Number(v||0);return '$'+n.toFixed(2);}
 function odds(v){const p=Number(v);if(!Number.isFinite(p)||p<=0||p>=1)return '—';const cents=p*100;const ct=Math.abs(cents-Math.round(cents))<0.05?String(Math.round(cents)):cents.toFixed(1);return (1/p).toFixed(2)+' ('+ct+'¢)';}
 function sourceSport(x){
  let raw=String(x.strategy_source||x.attribution_source||x.capper||x.source_name||'').trim();
  let sport=String(x.strategy_sport||x.sport||x.league||'').trim().toUpperCase();
  const m=raw.match(/^(.*?)\s+-\s+(NFL|CFB|WNBA|NBA|MLB|NHL|SOCCER|UFC|NRL|WNRL)$/i);
  if(m){
   raw=m[1].trim();
   if(!sport)sport=m[2].toUpperCase();
  }
  if(!raw){
   const src=String(x.source||'').trim();
   if(src==='slack_live')raw='PW';
   else if(src==='termux_executor')raw='SH01';
   else raw=src;
  }
  return {source:raw||'—',sport:sport||'—'};
 }

 /* Re-render the compact Portfolio list with source + sport on every live position. */
 window.renderPortfolioLivePositions=function(rows){
  const box=document.getElementById('portfolioLivePositions');
  const list=document.getElementById('portfolioLivePositionsList');
  const walletMain=document.querySelector('.wallet-main');
  if(box&&walletMain&&box.parentElement!==walletMain)walletMain.appendChild(box);
  if(!list)return;
  const live=(Array.isArray(rows)?rows:[])
   .filter(x=>!x.paper)
   .sort((a,b)=>new Date(b.submitted_at||b.created_at||0)-new Date(a.submitted_at||a.created_at||0));
  if(!live.length){list.innerHTML='<div class="portfolio-live-empty">No live positions.</div>';return;}
  list.innerHTML=live.map(x=>{
   const bet=x.exact_position||x.selection||x.outcome||x.market||'—';
   const stake=x.actual_cost_usdc??x.budget_usdc??0;
   const shares=x.remaining_shares??x.shares;
   const pnl=Number(x.estimated_pnl||0);
   const pnlClass=pnl>0?'positive':pnl<0?'negative':'';
   const pnlText=(pnl>0?'+':'')+money(pnl);
   const sh=(shares===null||shares===undefined||shares==='')?'—':Number(shares).toFixed(2);
   const id=sourceSport(x);
   return '<div class="portfolio-live-line"><span class="portfolio-live-bet">'+esc(bet)+'</span>'
    +' · Source <span class="portfolio-live-source">'+esc(id.source)+'</span>'
    +' · Sport <span class="portfolio-live-sport">'+esc(id.sport)+'</span>'
    +' · Stake '+money(stake)
    +' · Odds '+odds(x.entry_price)
    +' · Shares '+sh
    +' · P/L <span class="portfolio-live-pnl '+pnlClass+'">'+pnlText+'</span></div>';
  }).join('');
 };

 window.s01807JumpOther=jumpOtherToCurrent;
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
