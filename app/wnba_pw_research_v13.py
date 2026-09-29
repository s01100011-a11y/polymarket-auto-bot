from app import wnba_pw_strategy_test_v12 as base
from app import pw_research_sync
from app import pw_scalping_research
from app import pw_market_research
from app import pw_spread_capture
from app import pw_game_reconstruction
from app import pw_spread_backtest
from app import nfl_capper_ingest
from app import cfb_capper_preview
from app import basketball_monitor_capper

app = base.app
history = base.history
dashboard = base.dashboard
core = base.core
ingest = base.ingest

pw_research_sync.install(
    app=app,
    history=history,
    core=core,
    dashboard=dashboard,
)

pw_scalping_research.install(
    app=app,
    history=history,
    dashboard=dashboard,
)

pw_market_research.install(
    app=app,
    history=history,
    ingest=ingest,
    dashboard=dashboard,
    strategy=base,
)

pw_spread_capture.install(
    app=app,
    history=history,
    ingest=ingest,
    dashboard=dashboard,
    strategy=base,
)

pw_game_reconstruction.install(
    app=app,
    history=history,
    dashboard=dashboard,
    ingest=ingest,
)

pw_spread_backtest.install(
    history=history,
    ingest=ingest,
)

nfl_capper_ingest.install(
    app=app,
    dashboard=dashboard,
    core=core,
)

cfb_capper_preview.install(
    app=app,
    dashboard=dashboard,
    core=core,
)

basketball_monitor_capper.install(
    app=app,
    dashboard=dashboard,
    core=core,
    ingest=ingest,
    nfl=nfl_capper_ingest,
)

def _install_s01807_win95_theme() -> None:
    html = dashboard.DASHBOARD_HTML
    if "s01807-win95-theme" in html:
        return

    html = html.replace(
        "<title>Polymarket Bot Dashboard</title>",
        "<title>s01807.exe</title>",
        1,
    )
    old_top = '''  <div class="top">
    <div><div class="eyebrow">Railway · Polymarket</div><div class="title">Trading Bot Dashboard</div><div class="versionline"><span class="versionchip" id="versionLabel">v—</span><span class="versionchip" id="versionDateLabel">Version date —</span></div><div class="sub" id="updated">Loading status…</div></div>
    <div class="badge"><span class="dot"></span><span id="serviceState">Connecting</span></div>
  </div>'''
    new_top = '''  <div class="top win95-app-chrome">
    <div class="win95-titlebar">
      <div class="win95-title-left">
        <span class="win95-logo" aria-hidden="true"><i class="win95-logo-red"></i><i class="win95-logo-green"></i><i class="win95-logo-blue"></i><i class="win95-logo-yellow"></i></span>
        <span class="win95-title-text" id="win95Title">S01807 v—</span>
      </div>
      <div class="win95-window-controls" aria-hidden="true">
        <span class="win95-window-control win95-minimize">_</span>
        <span class="win95-window-control win95-maximize">□</span>
        <span class="win95-window-control win95-close">×</span>
      </div>
    </div>
    <div class="win95-info-strip">
      <div class="win95-info-group">
        <span class="versionchip" id="versionDateLabel">Version date —</span>
      </div>
      <div class="win95-info-group win95-info-right">
        <span class="sub" id="updated">Loading status…</span>
        <button type="button" class="badge" id="botPowerBtn" data-enabled="1" aria-pressed="true" title="Dashboard master bot switch"><span class="dot"></span><span id="serviceState">Connecting</span></button>
      </div>
    </div>
  </div>'''
    html = html.replace(old_top, new_top, 1)

    theme_css = r"""
/* s01807-win95-theme */
:root{
 color-scheme:light;
 --bg:#008080;
 --panel:#c0c0c0;
 --panel2:#000000;
 --border:#808080;
 --text:#000000;
 --muted:#404040;
 --accent:#00aa55;
 --warn:#b8860b;
 --bad:#cc0000;
 --blue:#000080;
}
*{border-radius:0!important}
html{background:#008080}
body{
 background:#008080;
 color:#000;
 font-family:"MS Sans Serif",Tahoma,Arial,sans-serif;
 font-size:13px;
}
a{color:#0000ee;text-decoration:underline}
.wrap{
 max-width:1440px;
 margin:16px auto 54px;
 padding:0;
 background:transparent;
 border:0;
 box-shadow:none;
}
.top.win95-app-chrome{
 display:block!important;
 margin:0 0 4px!important;
 padding:2px!important;
 min-height:0!important;
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-sizing:border-box;
}
.win95-titlebar{
 min-height:27px;
 display:flex;
 align-items:center;
 justify-content:space-between;
 gap:8px;
 padding:2px 3px 2px 4px;
 background:linear-gradient(90deg,#000080 0%,#1084d0 76%,#000080 100%);
 color:#fff;
 overflow:hidden;
}
.win95-title-left{
 display:flex;
 align-items:center;
 gap:6px;
 min-width:0;
}
.win95-title-text{
 min-width:0;
 overflow:hidden;
 text-overflow:ellipsis;
 white-space:nowrap;
 font-family:"MS Sans Serif",Tahoma,Arial,sans-serif;
 font-size:13px;
 line-height:20px;
 font-weight:800;
 letter-spacing:0;
 text-shadow:1px 1px #000;
}
.win95-logo{
 flex:0 0 18px;
 width:18px;
 height:18px;
 display:grid;
 grid-template-columns:1fr 1fr;
 grid-template-rows:1fr 1fr;
 gap:1px;
 padding:1px;
 background:#fff;
 border:1px solid #000;
 box-shadow:1px 1px 0 rgba(0,0,0,.45);
 transform:skewY(-4deg);
 box-sizing:border-box;
}
.win95-logo i{display:block;min-width:0;min-height:0}
.win95-logo-red{background:#f22}
.win95-logo-green{background:#0a5}
.win95-logo-blue{background:#1683ff}
.win95-logo-yellow{background:#ffd400}
.win95-window-controls{
 flex:0 0 auto;
 display:flex;
 align-items:center;
 gap:2px;
}
.win95-window-control{
 width:22px;
 height:20px;
 display:flex;
 align-items:center;
 justify-content:center;
 box-sizing:border-box;
 background:#c0c0c0;
 color:#000;
 border-top:2px solid #fff;
 border-left:2px solid #fff;
 border-right:2px solid #404040;
 border-bottom:2px solid #404040;
 box-shadow:1px 1px 0 #000;
 font-family:Tahoma,Arial,sans-serif;
 font-size:13px;
 line-height:14px;
 font-weight:900;
 user-select:none;
 cursor:default;
}
.win95-minimize{align-items:flex-end;padding-bottom:2px}
.win95-maximize{font-size:12px}
.win95-close{font-size:15px}
.win95-info-strip{
 display:flex;
 align-items:center;
 justify-content:space-between;
 gap:8px;
 flex-wrap:wrap;
 padding:4px 5px 3px;
 background:#c0c0c0;
 border-top:1px solid #dfdfdf;
 color:#000;
}
.win95-info-group{
 display:flex;
 align-items:center;
 gap:4px;
 flex-wrap:wrap;
 min-width:0;
}
.win95-info-right{margin-left:auto;justify-content:flex-end}
.win95-info-cell{
 display:inline-flex;
 align-items:center;
 min-height:20px;
 padding:2px 6px;
 box-sizing:border-box;
 background:#d4d0c8;
 color:#000;
 border-top:1px solid #808080;
 border-left:1px solid #808080;
 border-right:1px solid #fff;
 border-bottom:1px solid #fff;
 font-size:10px;
 font-weight:800;
 letter-spacing:.02em;
 white-space:nowrap;
}
.eyebrow{display:none!important}
.title{display:none!important}
.sub{color:#404040!important;font-size:10px}
.versionline{margin:0;gap:4px}
.versionchip,.badge{
 background:#c0c0c0;
 color:#000;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 padding:3px 7px;
 font-size:10px;
 box-shadow:1px 1px 0 #000;
}
.badge{border-radius:0!important;cursor:pointer}
.badge.offline .dot{background:#ff1616!important;border-color:#600!important}
.badge.offline #serviceState{color:#900!important}
.dot{
 width:8px;height:8px;
 background:#00ff66;
 border:1px solid #004000;
 box-shadow:none;
}
.cards{gap:10px;margin:10px 0}.performance-strip{gap:4px;margin:4px 0}
.card,.performance-card,.wallet-strip,.panel,.nfl-capper-panel,.more-stats-panel{
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
}
.card,.performance-card{padding:9px!important}
.label{
 color:#fff!important;
 font-size:10px!important;
 letter-spacing:.04em!important;
 font-weight:700!important;
}
.value,.performance-value{
 color:#fff;
 font-family:"Courier New",monospace;
 font-weight:900;
}
.green,.positive,.performance-value.green{color:#008000!important}
.red,.negative,.performance-value.red{color:#b00000!important}
.yellow{color:#8a5b00!important}
.wallet-live-layout{gap:10px;margin:10px 0!important}
.wallet-strip{padding:2px!important;overflow:hidden}
.wallet-main{padding:8px 9px 10px;background:#000!important;color:#fff!important}
.wallet-main>.label{display:none!important}
.win95-section-titlebar{
 min-height:24px;
 display:flex;
 align-items:center;
 justify-content:space-between;
 gap:8px;
 padding:2px 3px 2px 5px;
 background:linear-gradient(90deg,#000080 0%,#1084d0 78%,#000080 100%);
 color:#fff;
 box-sizing:border-box;
}
.win95-section-title{
 overflow:hidden;
 text-overflow:ellipsis;
 white-space:nowrap;
 font-size:11px;
 font-weight:800;
 text-shadow:1px 1px #000;
 letter-spacing:.02em;
}
.win95-mini-controls{display:flex;gap:2px;flex:0 0 auto}
.win95-mini-control{
 width:19px;
 height:17px;
 display:flex;
 align-items:center;
 justify-content:center;
 box-sizing:border-box;
 background:#c0c0c0;
 color:#000;
 border-top:2px solid #fff;
 border-left:2px solid #fff;
 border-right:2px solid #404040;
 border-bottom:2px solid #404040;
 box-shadow:1px 1px 0 #000;
 font:900 11px/11px Tahoma,Arial,sans-serif;
 user-select:none;
 cursor:default;
}
.win95-mini-control:first-child{align-items:flex-end;padding-bottom:1px}
.win95-stats-window{
 margin:10px 0;
 padding:2px;
 background:#c0c0c0;
 border-top:2px solid #fff;
 border-left:2px solid #fff;
 border-right:2px solid #404040;
 border-bottom:2px solid #404040;
 box-shadow:1px 1px 0 #000;
}
.win95-stats-body{padding:6px 8px 8px;background:#000!important;color:#fff!important}
.win95-stats-body #statsModeFilter{margin:0 0 6px}
.win95-stats-body #statsModeFilter .mode-filter-label{display:none}
.win95-stats-body .performance-strip{margin:0 0 6px}
.win95-stats-body .more-stats-shell{margin:0}

.wallet-address{font-family:"Courier New",monospace;color:#fff!important}
.wallet-state{color:#fff!important}
.wallet-metrics-stack{border-top:1px solid #808080!important;margin-top:8px!important}
.wallet-metric{border-color:#808080!important;padding:9px 0!important}
.wallet-balance{color:#008000!important;font-family:"Courier New",monospace!important}
.wallet-secondary{color:#fff!important;font-family:"Courier New",monospace!important}
.pnl-chart-card{
 background:#000!important;
 color:#00ff66!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
 padding:0!important;
}
.pnl-chart-head{
 margin:0!important;
 padding:4px 6px!important;
 align-items:center!important;
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 border-bottom:2px solid #808080!important;
}
.pnl-chart-head .label{color:#fff!important;text-shadow:1px 1px #000}
.pnl-chart-value{font-family:"Courier New",monospace;color:#00ff66!important}
.pnl-chart-range{color:#e0e0e0!important}
.pnl-chart{background:#000!important;margin:0!important;padding:6px!important}
.pnl-axis{fill:#a0a0a0!important}
.pnl-zero{stroke:#808080!important}
.tabs,.more-stats-tabs{gap:3px;margin:6px 0}
button,.tab,.btn,.toggle-btn,.paper-close-btn,.live-sell-btn,.sell-btn,.more-stats-main-btn{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
 padding:5px 9px!important;
 min-height:28px;
 font-family:"MS Sans Serif",Tahoma,Arial,sans-serif!important;
 font-weight:700!important;
 text-transform:none!important;
 cursor:pointer;
}
button:active,button.active,button[aria-pressed="true"],.tab.active,.toggle-btn.active,.more-stats-tabs button.active{
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-shadow:none!important;
 transform:translate(1px,1px);
 background:#d4d0c8!important;
 color:#000!important;
}
button:disabled{color:#808080!important;text-shadow:1px 1px #fff!important}
.panel{
 padding:8px!important;
 display:none;
 margin-top:10px;
}
.panel.active{display:block}
.panel h2{
 margin:-6px -6px 7px!important;
 padding:4px 6px!important;
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 font-size:13px!important;
 line-height:1.2!important;
 text-shadow:1px 1px #000;
}
.note,.muted,.performance-sub,.toggle-note,.foot,.setting label,.exec-event .evtime,.mode-filter-label,.slack-mode-note,.slack-mode-controls label,.slack-mode-state{color:#fff!important}
.table-wrap{
 background:#000!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
table{background:#000;color:#e8e8e8}
th{
 background:#000080!important;
 color:#fff!important;
 border-right:1px solid #c0c0c0!important;
 border-bottom:2px solid #c0c0c0!important;
 font-size:10px!important;
 letter-spacing:.02em!important;
}
td{
 background:#000!important;
 color:#d8d8d8!important;
 border-bottom:1px solid #303030!important;
 padding:7px 8px!important;
}
td.green,.nfl-result-line.positive,.cfb-result-line.positive{color:#00ff66!important}
td.red,.nfl-result-line.negative,.cfb-result-line.negative{color:#ff6060!important}
.status{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:1px solid #fff!important;
 border-left:1px solid #fff!important;
 border-right:1px solid #404040!important;
 border-bottom:1px solid #404040!important;
 padding:2px 5px!important;
}
.setting,.ro,.exec-status-box{
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #808080!important;
 border-left:2px solid #808080!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.setting input,select,input{
 background:#fff!important;
 color:#000!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.exec-event{
 background:#000!important;
 color:#fff!important;
 border:1px solid #808080!important;
}
.nfl-capper-panel{
 padding:8px!important;
 margin:10px 0!important;
}
.nfl-capper-head{
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 margin:-6px -6px 0!important;
 padding:5px 6px!important;
 display:flex!important;
 flex-direction:row!important;
 align-items:center!important;
 justify-content:flex-start!important;
 text-align:left!important;
}
.nfl-capper-head .label,.nfl-capper-head .capper-panel-title{color:#fff!important;text-align:left!important}
.capper-panel-title{font-size:15px!important;font-weight:900!important;line-height:1.2!important;text-transform:uppercase}
.capper-panel-status-row{
 display:flex!important;
 align-items:flex-end!important;
 justify-content:space-between!important;
 gap:8px!important;
 padding:7px 0 5px!important;
 text-align:left!important;
}
.capper-status-field{flex:1 1 auto;min-width:0}
.capper-status-label{
 display:block!important;
 margin:0 0 3px!important;
 color:#000!important;
 font:700 11px/1.2 "MS Sans Serif",Tahoma,Arial,sans-serif!important;
 text-transform:uppercase!important;
 letter-spacing:.03em!important;
}
.nfl-capper-state{
 min-height:30px!important;
 display:flex!important;
 align-items:center!important;
 box-sizing:border-box!important;
 padding:5px 8px!important;
 background:#fff!important;
 color:#000!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 text-shadow:none!important;
 text-align:left!important;
 margin:0!important;
 font-weight:900!important;
}
.nfl-capper-meta{width:100%;text-align:left!important;color:#000!important}
.capper-panel-actions{justify-content:flex-start!important;margin-top:5px!important}
.sport-power-btn{margin-left:auto!important}
.nfl-capper-card{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
}
.more-stats-row{
 background:#000!important;
 color:#d8d8d8!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-shadow:none!important;
}
.nfl-capper-card>b,.capper-card-head>b{color:#000!important}
.more-stats-row .name{color:#fff!important}
.nfl-capper-kpis,.cfb-capper-performance{color:#000!important}
.nfl-position-row,.cfb-open-position,.cfb-settled-position{
 background:#050505!important;
 color:#d8d8d8!important;
 border-radius:0!important;
}
.nfl-position-row.win,.cfb-settled-position.win{border-left:4px solid #00c000!important}
.nfl-position-row.loss,.cfb-settled-position.loss{border-left:4px solid #d00000!important}
.nfl-position-row.open,.cfb-open-position{border-left:4px solid #d0d000!important}
.more-stats-panel{padding:8px!important}
.more-stats-head{
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 margin:-6px -6px 8px!important;
 padding:4px 6px!important;
}
.more-stats-head .label,.more-stats-head .performance-sub{color:#fff!important}
.empty{color:#fff!important;background:#000!important}
.win95-stats-window .more-stats-panel{margin-top:8px!important}

/* Dope Wars-inspired retro data typography + palette */
:root{
 --retro-green:#00ff2a;
 --retro-red:#ff1616;
 --retro-yellow:#ffff2e;
 --retro-blue:#315cff;
 --retro-cyan:#00e5ff;
 --retro-white:#f4f4f4;
 --retro-gray:#ffffff;
}
.value,.performance-value,.wallet-address,.wallet-balance,.wallet-secondary,
.pnl-chart-value,.pnl-chart-range,.nfl-capper-kpis,.cfb-capper-performance,
.nfl-position-title,.nfl-position-pnl,.cfb-result-line,.monitor-performance,
.monitor-feed,.monitor-meta,.monitor-action,.more-stats-row,.more-stats-row .pnl,
table,th,td,.status{
 font-family:"Lucida Console","Courier New",monospace!important;
 font-variant-numeric:tabular-nums;
 letter-spacing:.01em;
}
.value,.performance-value,.wallet-balance,.wallet-secondary,.pnl-chart-value,
.nfl-position-pnl,.cfb-result-line,.more-stats-row .pnl{
 font-weight:900!important;
}
.green,.positive,.performance-value.green,.wallet-balance,
td.green,.nfl-result-line.positive,.cfb-result-line.positive{
 color:var(--retro-green)!important;
}
.red,.negative,.performance-value.red,
td.red,.nfl-result-line.negative,.cfb-result-line.negative{
 color:var(--retro-red)!important;
}
.yellow,.warn,.warning{color:var(--retro-yellow)!important}
.blue{color:var(--retro-blue)!important}
.pnl-chart-value.green{color:var(--retro-green)!important}
.pnl-chart-value.red{color:var(--retro-red)!important}
.pnl-chart-body .label{color:var(--retro-yellow)!important}
.pnl-chart-range{color:var(--retro-gray)!important}
.pnl-axis{fill:var(--retro-gray)!important;font-family:"Lucida Console","Courier New",monospace!important}
.more-stats-row{color:var(--retro-gray)!important}
.nfl-capper-card{color:#000!important}
.nfl-capper-card>b,.capper-card-head>b{
 color:#000!important;
 font-family:"Lucida Console","Courier New",monospace!important;
 letter-spacing:.02em;
}
.more-stats-row .name{
 color:var(--retro-white)!important;
 font-family:"Lucida Console","Courier New",monospace!important;
 letter-spacing:.02em;
}
.capper-card-head{display:flex!important;align-items:center!important;justify-content:space-between!important;gap:7px!important;text-align:left!important}
.capper-power-btn{flex:0 0 auto!important;margin:0!important;white-space:nowrap!important}
.capper-power-btn.offline .dot{background:#ff1616!important;border-color:#600!important}
.capper-power-btn.offline .capper-power-text{color:#900!important}
.nfl-capper-kpis,.cfb-capper-performance{color:#000!important}
.nfl-position-row,.cfb-open-position,.cfb-settled-position{
 font-family:"Lucida Console","Courier New",monospace!important;
}
.nfl-position-row.win .nfl-position-title,.cfb-settled-position.win b{color:var(--retro-green)!important}
.nfl-position-row.loss .nfl-position-title,.cfb-settled-position.loss b{color:var(--retro-red)!important}
.nfl-position-row.open .nfl-position-title,.cfb-open-position b{color:var(--retro-yellow)!important}
.more-stats-row .positive{color:var(--retro-green)!important}
.more-stats-row .negative{color:var(--retro-red)!important}
.monitor-signal{
 background:#000!important;
 color:var(--retro-gray)!important;
 font-family:"Lucida Console","Courier New",monospace!important;
}
.monitor-signal b{color:var(--retro-white)!important}
.monitor-action.positive{color:var(--retro-green)!important}
.monitor-action.negative{color:var(--retro-red)!important}
.monitor-action.flat{color:var(--retro-yellow)!important}
.table-wrap td{color:var(--retro-gray)!important}
.table-wrap td.green{color:var(--retro-green)!important}
.table-wrap td.red{color:var(--retro-red)!important}

/* Win95 grey window bodies with inset black data boxes */
.wallet-strip,.panel,.nfl-capper-panel,.more-stats-panel,.pnl-chart-card{
 background:#c0c0c0!important;
 color:#000!important;
}
.wallet-main,.win95-stats-body{
 background:#c0c0c0!important;
 color:#000!important;
}
.wallet-data-box,.performance-card,.more-stats-row,
.setting,.ro,.exec-status-box,.monitor-signal,.table-wrap{
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-sizing:border-box;
}
.nfl-capper-card{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-sizing:border-box;
}
.wallet-dashboard-row{gap:7px!important;margin-top:7px!important}
.wallet-data-box{padding:9px!important}
.wallet-data-box .label,.performance-card .label,
.more-stats-row .label,.setting .label,.ro .label,.exec-status-box .label,
.monitor-signal .label,.table-wrap .label{
 color:#fff!important;
}
.nfl-capper-card .label{color:#000!important}
.wallet-data-box .wallet-state,.wallet-data-box .performance-sub,
.performance-card .performance-sub,
.more-stats-row .muted,.setting label,.ro .toggle-note,.exec-status-box .note{
 color:#fff!important;
}
.nfl-capper-card .muted{color:#000!important}
.wallet-address,.wallet-state,.wallet-secondary{color:#fff!important}
.win95-stats-window{background:#c0c0c0!important}
.win95-stats-body{padding:7px!important}
.win95-stats-body .performance-strip{gap:7px!important;margin:0 0 7px!important}
.more-stats-panel{padding:7px!important}
.more-stats-grid{gap:7px!important}
.pnl-chart-card{padding:0!important}
.pnl-chart-body{
 margin:7px!important;
 padding:8px!important;
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-sizing:border-box;
}
.pnl-chart-body .label{color:var(--retro-yellow)!important}
.pnl-chart{background:#000!important}
.nfl-capper-panel{padding:8px!important}
.nfl-capper-grid{gap:7px!important}
.nfl-capper-card{padding:9px!important}
.panel{padding:8px!important}
.panel>.note{color:#000!important}
.panel .exec-status-box .note{color:#fff!important}
.empty{background:#000!important;color:#fff!important}

/* Normalize legacy PW/research/control panels to the Win95 dashboard system */
.slack-mode-box{
 margin:10px 0!important;
 padding:2px!important;
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
}
.slack-mode-head{
 margin:0!important;
 padding:5px 6px!important;
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 border-bottom:2px solid #808080!important;
}
.slack-mode-head .label,
.slack-mode-head .slack-mode-value,
.slack-mode-head .slack-mode-state{
 color:#fff!important;
 text-shadow:1px 1px #000;
}
.slack-mode-value,.slack-mode-state,.slack-mode-note,
.pw-strategy-name,.pw-strategy-rule,.pw-strategy-section-title,
.pw-strategy-metric-label,.pw-strategy-metric-value,.pw-strategy-sub,
.pw-strategy-loading,.pw-filter-final,.pw-filter-table,
.live-test-lock,.live-test-result,.live-test-warning{
 font-family:"Lucida Console","Courier New",monospace!important;
 font-variant-numeric:tabular-nums;
}
.pw-strategy-total{margin:7px!important}
.pw-strategy-cards{
 gap:7px!important;
 margin:7px!important;
}
.pw-strategy-card{
 min-width:0!important;
 padding:9px!important;
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-shadow:none!important;
}
.pw-strategy-total .pw-strategy-card{
 background:#000!important;
 border-color:initial!important;
}
.pw-strategy-card-head{gap:8px!important;margin-bottom:7px!important}
.pw-strategy-name{color:#fff!important;font-size:15px!important;font-weight:900!important}
.pw-strategy-rule{color:#fff!important;font-size:10px!important;line-height:1.45!important}
.pw-strategy-section{
 border-top:1px solid #555!important;
 margin-top:8px!important;
 padding-top:8px!important;
}
.pw-strategy-section-title{
 color:#fff!important;
 font-size:10px!important;
 letter-spacing:.04em!important;
}
.pw-strategy-metrics{gap:5px!important}
.pw-strategy-metric{
 min-width:0!important;
 padding:7px!important;
 background:#080808!important;
 color:#fff!important;
 border:1px solid #555!important;
}
.pw-strategy-metric-label{color:#fff!important}
.pw-strategy-metric-value{color:#fff!important;font-weight:900!important}
.pw-strategy-metric-value.green{color:var(--retro-green)!important}
.pw-strategy-metric-value.red{color:var(--retro-red)!important}
.pw-strategy-sub,.pw-strategy-loading{color:#fff!important}
.pw-strategy-badge,
.pw-filter-badge{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
}
.pw-strategy-badge.on,.pw-filter-badge.on{
 color:#006000!important;
 font-weight:900!important;
}
.pw-strategy-badge.off,.pw-filter-badge.off{
 color:#a00000!important;
 font-weight:900!important;
}
.pw-filter-table-wrap{
 margin:7px!important;
 background:#000!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.pw-filter-table{background:#000!important;color:#fff!important}
.pw-filter-table th{
 background:#000080!important;
 color:#fff!important;
 border-color:#808080!important;
}
.pw-filter-table td{
 background:#000!important;
 color:#fff!important;
 border-color:#333!important;
}
.pw-filter-profit{font-weight:900!important}
.pw-filter-final{
 margin:7px!important;
 padding:9px!important;
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.slack-mode-note{
 margin:7px!important;
 padding:8px!important;
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.slack-mode-controls,
.slack-pending{margin:7px!important}
.slack-pending-row{
 background:#000!important;
 color:#fff!important;
 border:1px solid #555!important;
 padding:8px!important;
}
.slack-pending-row .muted{color:#fff!important}
.live-test-warning,.live-test-lock,.live-test-result{
 margin:7px!important;
 padding:9px!important;
 background:#000!important;
 color:#fff!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
}
.live-test-warning{color:var(--retro-red)!important}
.live-test-lock b{color:var(--retro-yellow)!important}
.live-test-result{white-space:pre-wrap;word-break:break-word}
.save-msg{color:#fff!important}
#pwStrategyPanel>.slack-mode-note,
#pwFilterPanel>.slack-mode-note{margin-top:7px!important}
#pwStrategyPanel .green,#pwFilterPanel .green{color:var(--retro-green)!important}
#pwStrategyPanel .red,#pwFilterPanel .red{color:var(--retro-red)!important}
@media(max-width:900px){
 .pw-strategy-cards{grid-template-columns:1fr!important}
 .pw-strategy-metrics{grid-template-columns:repeat(2,minmax(0,1fr))!important;overflow:visible!important}
}
@media(max-width:520px){
 .pw-strategy-metrics{grid-template-columns:repeat(2,minmax(0,1fr))!important}
 .pw-strategy-card{padding:8px!important}
 .pw-strategy-name{font-size:14px!important}
 .pw-strategy-rule,.pw-strategy-sub{font-size:10px!important}
}

/* Clean capper card information hierarchy */
.capper-sizing{
 margin:8px 0 10px;
 padding:7px;
 background:#101010;
 border:1px solid #555;
}
.capper-sizing-row{
 display:grid;
 grid-template-columns:110px minmax(0,1fr);
 align-items:center;
 gap:8px;
 padding:5px 0;
}
.capper-sizing-row+.capper-sizing-row{border-top:1px solid #333}
.capper-sizing-label{
 color:#fff;
 font:700 11px/1.2 "MS Sans Serif",Tahoma,Arial,sans-serif;
 text-transform:uppercase;
 letter-spacing:.03em;
}
.capper-sizing-controls{
 display:flex;
 align-items:center;
 gap:5px;
 min-width:0;
 flex-wrap:wrap;
}
.capper-sizing-controls input{
 width:84px!important;
 min-width:70px;
 box-sizing:border-box;
}
.capper-sizing-controls button{
 min-height:30px!important;
 padding:4px 8px!important;
}
.capper-sizing-note{
 margin-top:5px;
 padding-top:6px;
 border-top:1px solid #333;
 color:#fff;
 font:700 11px/1.45 "Lucida Console","Courier New",monospace;
}
.capper-sizing-note span{font-weight:400;color:#d8d8d8}
.capper-metrics-grid{
 display:grid;
 grid-template-columns:repeat(4,minmax(0,1fr));
 gap:6px;
 margin:9px 0 10px;
}
.capper-metric{
 min-width:0;
 padding:7px 8px;
 background:#080808;
 border:1px solid #3f3f3f;
}
.capper-metric>span{
 display:block;
 margin-bottom:3px;
 color:#bfbfbf;
 font:700 10px/1.2 "MS Sans Serif",Tahoma,Arial,sans-serif;
 text-transform:uppercase;
 letter-spacing:.03em;
}
.capper-metric>b{
 display:block;
 overflow-wrap:anywhere;
 color:#fff;
 font:700 14px/1.25 "Lucida Console","Courier New",monospace;
 font-variant-numeric:tabular-nums;
}
.capper-metric .capper-pnl{font-size:14px!important}
.capper-metric small{font-size:10px;font-weight:400;color:#bbb}
.capper-tabs{
 display:grid;
 grid-template-columns:repeat(5,minmax(0,1fr));
 gap:5px;
 margin-top:10px;
}
.capper-tabs button{
 min-width:0;
 min-height:34px!important;
 padding:5px 6px!important;
 display:flex;
 align-items:center;
 justify-content:center;
 gap:4px;
 opacity:.8;
}
.capper-tabs button.active{opacity:1}
.capper-tabs button span{
 overflow:hidden;
 text-overflow:ellipsis;
 white-space:nowrap;
}
.capper-tabs button b{font-size:11px}
.nfl-capper-card>b,.capper-card-head>b{
 display:block;
 margin-bottom:5px;
 font-size:20px!important;
}
@media(max-width:900px){
 .wrap{margin:0;padding:2px;box-shadow:none}
 .wallet-live-layout{grid-template-columns:1fr!important}
 .win95-info-strip{align-items:flex-start}
 .win95-info-right{margin-left:0;justify-content:flex-start}
}
@media(max-width:650px){
 .capper-metrics-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}
 .capper-tabs{grid-template-columns:repeat(3,minmax(0,1fr))}
 .capper-sizing-row{grid-template-columns:1fr;gap:4px}
 .capper-sizing-controls{display:grid;grid-template-columns:auto minmax(72px,1fr) auto;gap:5px}
 .capper-sizing-controls input{width:100%!important;min-width:0}
 .capper-sizing-controls button{white-space:nowrap}
 .nfl-capper-card{padding:10px!important}
 .nfl-capper-card>b,.capper-card-head>b{font-size:19px!important}
}
@media(max-width:520px){
 body{font-size:12px}
 .wrap{padding:2px}
 .win95-titlebar{min-height:25px}
 .win95-title-text{font-size:12px}
 .win95-logo{width:16px;height:16px;flex-basis:16px}
 .win95-window-control{width:20px;height:18px;font-size:12px}
 .win95-info-cell{white-space:normal}
 .card,.performance-card{padding:7px!important}
 .cards,.performance-strip{gap:3px}
}
"""
    html = html.replace("</style>", theme_css + "\n</style>", 1)

    chrome_js = r"""
function s01807MiniControls(){
 return '<span class="win95-mini-controls" aria-hidden="true"><span class="win95-mini-control">_</span><span class="win95-mini-control">□</span><span class="win95-mini-control">×</span></span>';
}
function s01807InstallSectionWindows(){
 const wallet=document.querySelector('.wallet-strip');
 if(wallet&&!wallet.querySelector(':scope > .win95-section-titlebar')){
  wallet.insertAdjacentHTML('afterbegin','<div class="win95-section-titlebar"><span class="win95-section-title">WALLET</span>'+s01807MiniControls()+'</div>');
 }

 const statsFilter=document.getElementById('statsModeFilter');
 const performance=document.querySelector('.performance-strip');
 const moreStats=document.querySelector('.more-stats-shell');
 if(statsFilter&&performance&&!statsFilter.closest('.win95-stats-window')){
  const parent=statsFilter.parentNode;
  const shell=document.createElement('div');
  shell.className='win95-stats-window';
  const bar=document.createElement('div');
  bar.className='win95-section-titlebar';
  bar.innerHTML='<span class="win95-section-title">STATS</span>'+s01807MiniControls();
  const body=document.createElement('div');
  body.className='win95-stats-body';
  parent.insertBefore(shell,statsFilter);
  shell.appendChild(bar);
  shell.appendChild(body);
  body.appendChild(statsFilter);
  body.appendChild(performance);
  if(moreStats)body.appendChild(moreStats);
 }
}
if(document.readyState==='loading'){
 document.addEventListener('DOMContentLoaded',s01807InstallSectionWindows);
}else{
 s01807InstallSectionWindows();
}
"""
    html = html.replace("</script>", chrome_js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_s01807_win95_theme()

