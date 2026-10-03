from __future__ import annotations

from app import dashboard_sh01_capper_v4 as base

dashboard = base.dashboard

html = dashboard.DASHBOARD_HTML
if "s01807TaskbarV1" not in html:
    css = r'''
.s01807-taskbar{position:fixed;top:0;left:0;right:0;z-index:10000;background:#c0c0c0;border-bottom:2px solid #404040;box-shadow:inset 0 1px #fff,inset 0 -1px #808080;display:flex;flex-direction:column;gap:3px;padding:3px 5px;box-sizing:border-box;font-family:"MS Sans Serif",Tahoma,Arial,sans-serif;font-size:12px;color:#000}.s01807-taskbar-row{display:flex;align-items:center;gap:3px;min-width:0;overflow-x:auto;overflow-y:hidden;white-space:nowrap;scrollbar-width:none}.s01807-taskbar-row::-webkit-scrollbar{display:none}.s01807-taskbar button{height:28px;display:inline-flex;align-items:center;justify-content:center;gap:5px;flex:1 1 0;min-width:74px;background:#c0c0c0;color:#000;border:2px outset #fff;padding:2px 7px;font:700 12px "MS Sans Serif",Tahoma,Arial,sans-serif;cursor:pointer}.s01807-taskbar button:active,.s01807-taskbar button.active{border-style:inset;background:#b8b8b8;padding-top:3px;padding-left:8px}.s01807-taskbar .app-icon{width:16px;height:16px;display:inline-block;flex:0 0 16px;image-rendering:pixelated}.s01807-taskbar svg{width:16px;height:16px;display:block;shape-rendering:crispEdges}.s01807-taskbar-spacer{height:65px}.s01807-task-anchor{scroll-margin-top:73px!important}@media(max-width:700px){.s01807-taskbar{padding:3px 4px}.s01807-taskbar button{height:27px;min-width:82px;padding-left:5px;padding-right:5px;font-size:11px}.s01807-taskbar-spacer{height:63px}.s01807-task-anchor{scroll-margin-top:71px!important}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    taskbar = r'''
<div class="s01807-taskbar" id="s01807TaskbarV1" role="navigation" aria-label="Dashboard quick navigation">
  <div class="s01807-taskbar-row">
    <button type="button" data-jump="top" title="S01807.exe — top"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="2" width="14" height="10" fill="#000"/><rect x="2" y="3" width="12" height="8" fill="#1b53a6"/><rect x="4" y="12" width="8" height="2" fill="#808080"/><rect x="3" y="14" width="10" height="1" fill="#000"/><rect x="4" y="5" width="6" height="1" fill="#fff"/><rect x="4" y="7" width="4" height="1" fill="#fff"/></svg></span>S01807.exe</button>
    <button type="button" data-jump="stats" title="Performance stats"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><rect x="1" y="1" width="14" height="14" fill="#fff" stroke="#000"/><rect x="3" y="9" width="2" height="4" fill="#1b53a6"/><rect x="7" y="6" width="2" height="7" fill="#008080"/><rect x="11" y="3" width="2" height="10" fill="#800080"/><path d="M2 13h12" stroke="#000"/></svg></span>Stats</button>
    <button type="button" data-jump="nfl" title="NFL"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="8" rx="6" ry="4" fill="#8b4513" stroke="#000" transform="rotate(-28 8 8)"/><path d="M6 6l4 4M7 5l4 4M5 7l4 4" stroke="#fff" stroke-width="1"/></svg></span>NFL</button>
    <button type="button" data-jump="cfb" title="College football"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3h10l2 3-2 3H2z" fill="#1b53a6" stroke="#000"/><rect x="2" y="9" width="1" height="6" fill="#000"/><ellipse cx="9.5" cy="11.5" rx="4" ry="2.5" fill="#8b4513" stroke="#000" transform="rotate(-25 9.5 11.5)"/><path d="M8 10l3 3" stroke="#fff"/></svg></span>CFB</button>
  </div>
  <div class="s01807-taskbar-row">
    <button type="button" data-jump="wnba" title="WNBA"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#f28c28" stroke="#000"/><path d="M1 8h14M8 1c-2 4-2 10 0 14M8 1c2 4 2 10 0 14M3 3c3 2 7 2 10 0M3 13c3-2 7-2 10 0" stroke="#000" stroke-width="1"/><rect x="10" y="1" width="5" height="5" fill="#800080" stroke="#000"/><text x="12.5" y="5" text-anchor="middle" font-size="4" font-family="Arial" fill="#fff">W</text></svg></span>WNBA</button>
    <button type="button" data-jump="nba" title="NBA"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="#f28c28" stroke="#000"/><path d="M1 8h14M8 1c-2 4-2 10 0 14M8 1c2 4 2 10 0 14M3 3c3 2 7 2 10 0M3 13c3-2 7-2 10 0" stroke="#000" stroke-width="1"/><rect x="10" y="1" width="5" height="5" fill="#1b53a6" stroke="#000"/><text x="12.5" y="5" text-anchor="middle" font-size="4" font-family="Arial" fill="#fff">N</text></svg></span>NBA</button>
    <button type="button" data-jump="other" title="Other / trade history"><span class="app-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 4h6l2 2h6v8H1z" fill="#f4d35e" stroke="#000"/><path d="M1 6h14" stroke="#000"/><path d="M4 10h7M4 12h5" stroke="#1b53a6"/></svg></span>Other</button>
  </div>
</div>
<div class="s01807-taskbar-spacer" aria-hidden="true"></div>
'''
    html = html.replace("<body>", "<body>" + taskbar, 1)

    js = r'''
<script>
(function(){
 const bar=document.getElementById('s01807TaskbarV1');if(!bar)return;
 const nearestPanel=el=>{if(!el)return null;return el.closest('.nfl-capper-panel,.window,.panel,.basketball-monitor-section,.performance-strip,.wrap')||el};
 const byText=(needle)=>[...document.querySelectorAll('.capper-panel-title,h1,h2,h3,.title,.window-title,.panel-title,.tab,button')].find(el=>String(el.textContent||'').trim().toLowerCase().includes(needle));
 const target=(key)=>{
  if(key==='top')return document.querySelector('.wrap')||document.body;
  if(key==='stats')return nearestPanel(document.getElementById('performancePortfolio')||document.getElementById('moreStatsBody')||byText('performance'));
  if(key==='nfl')return document.getElementById('nflCapperStats')||nearestPanel(byText('nfl auto-trading'));
  if(key==='cfb')return document.getElementById('cfbCapperStats')||nearestPanel(byText('cfb auto-trading'));
  if(key==='wnba')return document.getElementById('wnbaAutoTradingPanel')||nearestPanel(byText('wnba auto-trading'));
  if(key==='nba')return document.getElementById('nbaAutoTradingPanel')||nearestPanel(byText('nba auto-trading'));
  if(key==='other'){
    const history=document.querySelector('[data-tab="history"]');
    if(history&&typeof history.click==='function')history.click();
    return nearestPanel(history||document.getElementById('history')||byText('trade history')||byText('current trades'));
  }
  return null;
 };
 function activate(key){bar.querySelectorAll('button[data-jump]').forEach(b=>b.classList.toggle('active',b.dataset.jump===key&&key!=='top'));}
 function jump(key){
  const el=target(key);
  if(!el){console.warn('S01807 taskbar target not found:',key);return;}
  el.classList.add('s01807-task-anchor');
  activate(key);
  const y=Math.max(0,el.getBoundingClientRect().top+window.scrollY-bar.offsetHeight-7);
  window.scrollTo({top:y,behavior:'smooth'});
 }
 bar.addEventListener('click',e=>{const b=e.target.closest('button[data-jump]');if(b)jump(b.dataset.jump)});
 window.s01807TaskJump=jump;
})();
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
