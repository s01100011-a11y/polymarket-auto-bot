from __future__ import annotations

import re

from app import ufc_live_overview_v1 as live_overview


dashboard = live_overview.dashboard
html = dashboard.DASHBOARD_HTML

if "ufc-main-dashboard-cleanup-v1" not in html:
    # Remove browser-side UFC live workers from the heavy main dashboard. Their
    # backend routes / websocket cache stay loaded for the dedicated /ufc-live
    # page, but the main dashboard no longer polls/renders fights, prices,
    # volumes, timing, or per-fight UFC positions.
    for script_id in (
        "ufc-dashboard-v2",
        "ufc-fight-timing-v1",
        "ufc-price-stream-v1",
        "ufc-disabled-fast-action-v1",
        "ufc-position-summary-v1",
        "ufc-live-page-link-v1",
    ):
        html = re.sub(
            rf'<script\s+id=["\']{re.escape(script_id)}["\'][^>]*>.*?</script>',
            "",
            html,
            count=1,
            flags=re.S | re.I,
        )

    # Replace the original UFC fight/trading block and its own 30-second card
    # loader with a stats-only panel. The existing generic SH01 renderer still
    # sees title "UFC AUTO-TRADING" + nfl-capper-grid and therefore keeps SH01
    # UFC bets, W-L-P, ROI, realized/live P/L and position history visible.
    panel_marker = '<div class="nfl-capper-panel" id="ufcAutoTradingPanel">'
    base_script_marker = '<script id="ufcSh01DashboardV1">'
    start = html.find(panel_marker)
    script_start = html.find(base_script_marker, start if start >= 0 else 0)
    if start >= 0 and script_start >= 0:
        script_end = html.find("</script>", script_start)
        if script_end >= 0:
            script_end += len("</script>")
            stats_panel = r'''
<div class="nfl-capper-panel" id="ufcAutoTradingPanel">
  <div class="nfl-capper-head">
    <div class="capper-panel-title">UFC AUTO-TRADING</div>
  </div>
  <div class="capper-panel-status-row">
    <div class="capper-status-field"><div class="capper-status-label">STATUS</div><div class="nfl-capper-state">STATS ONLY</div></div>
    <div class="capper-status-field"><div class="capper-status-label">CAPPER</div><div class="nfl-capper-state">SH01</div></div>
    <div class="capper-status-field"><div class="capper-status-label">LIVE</div><div class="nfl-capper-state"><button type="button" onclick="location.href='/ufc-live'">OPEN UFC LIVE</button></div></div>
  </div>
  <div class="nfl-capper-grid"></div>
</div>
'''
            html = html[:start] + stats_panel + html[script_end:]

    # Lightweight UFC taskbar entry. Keep UFC directly below NBA on the main
    # dashboard and make the taskbar button jump to this stats panel. The fast
    # /ufc-live page remains available through OPEN UFC LIVE inside the panel.
    task_js = r'''
<script id="ufc-main-dashboard-cleanup-v1">
(function(){
 function placePanel(){
  const panel=document.getElementById('ufcAutoTradingPanel');
  const nba=document.getElementById('nbaAutoTradingPanel');
  if(panel&&nba&&panel.previousElementSibling!==nba)nba.insertAdjacentElement('afterend',panel);
 }
 function jumpToUfc(btn){
  const panel=document.getElementById('ufcAutoTradingPanel');if(!panel)return;
  const bar=document.getElementById('s01807TaskbarV1');
  if(bar)bar.querySelectorAll('button[data-jump]').forEach(x=>x.classList.toggle('active',x===btn));
  const offset=(bar?.offsetHeight||0)+7;
  const y=Math.max(0,panel.getBoundingClientRect().top+window.scrollY-offset);
  window.scrollTo({top:y,behavior:'smooth'});
 }
 function install(){
  placePanel();
  const bar=document.getElementById('s01807TaskbarV1');if(!bar)return;
  let b=bar.querySelector('button[data-jump="ufc"]');
  if(!b){
   const rows=bar.querySelectorAll('.s01807-taskbar-row'),row=rows[1];if(!row)return;
   row.classList.add('ufc-five');
   b=document.createElement('button');
   b.type='button';b.dataset.jump='ufc';b.className='ufc-task-btn';
   b.innerHTML='<span class="app-icon">🥊</span>UFC';
   const other=row.querySelector('[data-jump="other"]');other?row.insertBefore(b,other):row.appendChild(b);
  }
  b.title='UFC stats on main dashboard';
  b.onclick=null;
  b.addEventListener('click',function(e){e.preventDefault();e.stopImmediatePropagation();jumpToUfc(b)},true);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',install);else install();
 window.addEventListener('load',placePanel,{once:true});
})();
</script>
'''
    html = html.replace("</body>", task_js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
