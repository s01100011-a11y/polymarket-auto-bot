from app import wnba_pw_strategy_test_v12 as base
from app import pw_research_sync
from app import pw_scalping_research
from app import pw_market_research
from app import pw_spread_capture
from app import pw_game_reconstruction
from app import pw_spread_backtest
from app import nfl_capper_ingest
from app import cfb_capper_preview

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

def _install_s01807_win95_theme() -> None:
    html = dashboard.DASHBOARD_HTML
    if "s01807-win95-theme" in html:
        return

    html = html.replace(
        "<title>Polymarket Bot Dashboard</title>",
        "<title>S01-807 · Polymarket</title>",
        1,
    )
    html = html.replace(
        '<div class="eyebrow">Railway · Polymarket</div><div class="title">Trading Bot Dashboard</div>',
        '<div class="eyebrow">S01-807 · POLYMARKET SPORTS DESK</div><div class="title">S01-807</div>',
        1,
    )

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
 padding:3px;
 background:#c0c0c0;
 border-top:2px solid #fff;
 border-left:2px solid #fff;
 border-right:2px solid #404040;
 border-bottom:2px solid #404040;
 box-shadow:2px 2px 0 #000;
}
.top{
 margin:0 0 4px;
 padding:5px 6px;
 min-height:52px;
 background:linear-gradient(90deg,#000080 0%,#1084d0 72%,#000080 100%);
 color:#fff;
 align-items:center;
 border:0;
}
.eyebrow{
 color:#fff;
 font-size:11px;
 letter-spacing:.05em;
 text-shadow:1px 1px #000;
}
.title{
 color:#fff;
 font-family:"Courier New",monospace;
 font-size:27px;
 line-height:1;
 font-weight:900;
 letter-spacing:.04em;
 margin:2px 0 3px;
 text-shadow:1px 1px #000;
}
.sub{color:#e9e9e9;font-size:11px}
.versionline{margin-top:4px;gap:4px}
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
.badge{border-radius:0!important}
.dot{
 width:8px;height:8px;
 background:#00ff66;
 border:1px solid #004000;
 box-shadow:none;
}
.cards,.performance-strip{gap:4px;margin:4px 0}
.card,.performance-card,.wallet-strip,.panel,.nfl-capper-panel,.more-stats-panel{
 background:#c0c0c0!important;
 color:#000!important;
 border-top:2px solid #fff!important;
 border-left:2px solid #fff!important;
 border-right:2px solid #404040!important;
 border-bottom:2px solid #404040!important;
 box-shadow:1px 1px 0 #000!important;
}
.card,.performance-card{padding:9px!important}
.label{
 color:#303030!important;
 font-size:10px!important;
 letter-spacing:.04em!important;
 font-weight:700!important;
}
.value,.performance-value{
 color:#000;
 font-family:"Courier New",monospace;
 font-weight:900;
}
.green,.positive,.performance-value.green{color:#008000!important}
.red,.negative,.performance-value.red{color:#b00000!important}
.yellow{color:#8a5b00!important}
.wallet-live-layout{gap:4px;margin:4px 0!important}
.wallet-strip{padding:10px!important}
.wallet-address{font-family:"Courier New",monospace;color:#000!important}
.wallet-state{color:#404040!important}
.wallet-metrics-stack{border-top:1px solid #808080!important;margin-top:8px!important}
.wallet-metric{border-color:#808080!important;padding:9px 0!important}
.wallet-balance{color:#008000!important;font-family:"Courier New",monospace!important}
.wallet-secondary{color:#000!important;font-family:"Courier New",monospace!important}
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
button:active,.tab.active,.toggle-btn.active,.more-stats-tabs button.active{
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
 margin-top:4px;
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
.note,.muted,.performance-sub,.toggle-note,.foot{color:#404040!important}
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
 background:#c0c0c0!important;
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
 background:#fff!important;
 color:#000!important;
 border:1px solid #808080!important;
}
.nfl-capper-panel{
 padding:8px!important;
 margin:4px 0!important;
}
.nfl-capper-head{
 background:linear-gradient(90deg,#000080,#1084d0)!important;
 color:#fff!important;
 margin:-6px -6px 6px!important;
 padding:4px 6px!important;
 align-items:center!important;
}
.nfl-capper-head .label,.nfl-capper-head .nfl-capper-meta{color:#fff!important}
.nfl-capper-state{color:#fff!important;text-shadow:1px 1px #000}
.nfl-capper-card,.more-stats-row{
 background:#000!important;
 color:#d8d8d8!important;
 border-top:2px solid #404040!important;
 border-left:2px solid #404040!important;
 border-right:2px solid #fff!important;
 border-bottom:2px solid #fff!important;
 box-shadow:none!important;
}
.nfl-capper-card>b,.more-stats-row .name{color:#fff!important}
.nfl-capper-kpis,.cfb-capper-performance{color:#bcbcbc!important}
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
.empty{color:#808080!important;background:#000!important}
@media(max-width:900px){
 .wrap{margin:0;padding:2px;box-shadow:none}
 .wallet-live-layout{grid-template-columns:1fr!important}
 .top{align-items:flex-start}
}
@media(max-width:520px){
 body{font-size:12px}
 .wrap{padding:2px}
 .title{font-size:23px}
 .card,.performance-card{padding:7px!important}
 .cards,.performance-strip{gap:3px}
}
"""
    html = html.replace("</style>", theme_css + "\n</style>", 1)
    dashboard.DASHBOARD_HTML = html


_install_s01807_win95_theme()

