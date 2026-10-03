from __future__ import annotations

from app import dashboard_taskbar_v1 as base

dashboard = base.dashboard

html = dashboard.DASHBOARD_HTML
if "s01807TaskbarV2" not in html:
    css = r'''
/* s01807TaskbarV2 */
/* Leave a visible strip of the teal desktop between the fixed taskbar and the
   first dashboard window, including on the mobile layout where .wrap has no
   top margin. */
.s01807-taskbar-spacer{height:79px!important}
.s01807-title-icon{width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center;flex:0 0 16px;vertical-align:middle;image-rendering:pixelated}
.s01807-title-icon svg{width:16px;height:16px;display:block;shape-rendering:crispEdges}
.win95-title-left .s01807-title-icon{width:18px;height:18px;flex-basis:18px}.win95-title-left .s01807-title-icon svg{width:18px;height:18px}
.win95-section-title,.capper-panel-title,.panel h2,.pnl-chart-head .label,.portfolio-live-title{display:flex!important;align-items:center!important;gap:5px!important}
.capper-panel-title .s01807-title-icon,.win95-section-title .s01807-title-icon,.panel h2 .s01807-title-icon,.pnl-chart-head .label .s01807-title-icon,.portfolio-live-title .s01807-title-icon{flex:0 0 16px}
@media(max-width:700px){.s01807-taskbar-spacer{height:77px!important}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    js = r'''
<script id="s01807TaskbarV2">
(function(){
 const icons={
  dollar:'<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" fill="#fff" stroke="#000"/><rect x="3" y="3" width="10" height="10" fill="#006000"/><text x="8" y="11.5" text-anchor="middle" font-size="10" font-family="Arial" font-weight="900" fill="#fff">$</text></svg>',
  portfolio:'<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="4" width="14" height="10" fill="#7b4f21" stroke="#000"/><rect x="2" y="2" width="10" height="4" fill="#d2a45e" stroke="#000"/><rect x="8" y="7" width="7" height="4" fill="#c0c0c0" stroke="#000"/><rect x="10" y="8" width="2" height="2" fill="#000"/></svg>',
  stats:'<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" fill="#fff" stroke="#000"/><rect x="3" y="9" width="2" height="4" fill="#1b53a6"/><rect x="7" y="6" width="2" height="7" fill="#008080"/><rect x="11" y="3" width="2" height="10" fill="#800080"/><path d="M2 13h12" stroke="#000"/></svg>',
  nfl:'<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="8" rx="6" ry="4" fill="#8b4513" stroke="#000" transform="rotate(-28 8 8)"/><path d="M6 6l4 4M7 5l4 4M5 7l4 4" stroke="#fff" stroke-width="1"/></svg>',
  cfb:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3h10l2 3-2 3H2z" fill="#1b53a6" stroke="#000"/><rect x="2" y="9" width="1" height="6" fill="#000"/><ellipse cx="9.5" cy="11.5" rx="4" ry="2.5" fill="#8b4513" stroke="#000" transform="rotate(-25 9.5 11.5)"/><path d="M8 10l3 3" stroke="#fff"/></svg>',
  wnba:'<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#f28c28" stroke="#000"/><path d="M1 8h14M8 1c-2 4-2 10 0 14M8 1c2 4 2 10 0 14M3 3c3 2 7 2 10 0M3 13c3-2 7-2 10 0" stroke="#000" stroke-width="1"/><rect x="10" y="1" width="5" height="5" fill="#800080" stroke="#000"/><text x="12.5" y="5" text-anchor="middle" font-size="4" font-family="Arial" fill="#fff">W</text></svg>',
  nba:'<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#f28c28" stroke="#000"/><path d="M1 8h14M8 1c-2 4-2 10 0 14M8 1c2 4 2 10 0 14M3 3c3 2 7 2 10 0M3 13c3-2 7-2 10 0" stroke="#000" stroke-width="1"/><rect x="10" y="1" width="5" height="5" fill="#1b53a6" stroke="#000"/><text x="12.5" y="5" text-anchor="middle" font-size="4" font-family="Arial" fill="#fff">N</text></svg>',
  other:'<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 4h6l2 2h6v8H1z" fill="#f4d35e" stroke="#000"/><path d="M1 6h14" stroke="#000"/><path d="M4 10h7M4 12h5" stroke="#1b53a6"/></svg>'
 };
 const iconSpan=key=>'<span class="s01807-title-icon" aria-hidden="true">'+icons[key]+'</span>';
 function addIcon(el,key){if(!el||el.querySelector('.s01807-title-icon'))return;el.insertAdjacentHTML('afterbegin',iconSpan(key));}
 function installIcons(){
  /* S01807 uses the requested dollar-sign icon everywhere. */
  const taskTop=document.querySelector('#s01807TaskbarV1 button[data-jump="top"] .app-icon');
  if(taskTop)taskTop.innerHTML=icons.dollar;
  const oldMain=document.querySelector('.win95-title-left .win95-logo');
  if(oldMain)oldMain.outerHTML=iconSpan('dollar');

  addIcon(document.querySelector('.win95-section-title'),'stats');
  document.querySelectorAll('.capper-panel-title').forEach(el=>{
   const t=String(el.textContent||'').toUpperCase();
   if(t.includes('WNBA'))addIcon(el,'wnba');
   else if(t.includes('NBA'))addIcon(el,'nba');
   else if(t.includes('CFB'))addIcon(el,'cfb');
   else if(t.includes('NFL'))addIcon(el,'nfl');
  });
  addIcon(document.querySelector('.portfolio-live-title'),'portfolio');
  addIcon(document.querySelector('.pnl-chart-head .label'),'stats');
  addIcon(document.querySelector('#current h2'),'other');
  addIcon(document.querySelector('#history h2'),'other');
  addIcon(document.querySelector('#settings h2'),'other');
 }
 function jumpPortfolio(){
  const bar=document.getElementById('s01807TaskbarV1');
  const el=document.getElementById('portfolioLivePositions')||document.querySelector('.wallet-live-layout')||document.getElementById('walletBalance');
  if(!bar||!el)return;
  bar.querySelectorAll('button[data-jump]').forEach(b=>b.classList.toggle('active',b.dataset.jump==='portfolio'));
  const y=Math.max(0,el.getBoundingClientRect().top+window.scrollY-bar.offsetHeight-10);
  window.scrollTo({top:y,behavior:'smooth'});
 }
 function installPortfolioJump(){
  const bar=document.getElementById('s01807TaskbarV1');if(!bar)return;
  bar.addEventListener('click',function(e){
   const b=e.target.closest('button[data-jump="portfolio"]');if(!b)return;
   e.preventDefault();e.stopImmediatePropagation();jumpPortfolio();
  },true);
 }
 function init(){installIcons();installPortfolioJump();requestAnimationFrame(installIcons);setTimeout(installIcons,250);}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
 window.s01807JumpPortfolio=jumpPortfolio;
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
