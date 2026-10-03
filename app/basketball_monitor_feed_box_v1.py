from __future__ import annotations

from app import dashboard_sh01_capper_v3 as base


dashboard = base.dashboard
html = dashboard.DASHBOARD_HTML

if "basketballMonitorFeedBoxV1" not in html:
    css = r'''
/* WNBA/NBA monitor feed-health row: use the same dark inset treatment as the
   surrounding metric boxes instead of leaving status text on the grey panel. */
.monitor-feed{
 box-sizing:border-box!important;
 width:100%!important;
 margin:9px 0 10px!important;
 padding:7px 8px!important;
 background:#080808!important;
 border:1px solid #333!important;
 line-height:1.55!important;
 min-height:32px;
}
.monitor-feed.positive{color:#39ff6f!important}
.monitor-feed.negative{color:#ff5c5c!important}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace("</body>", '<script>const basketballMonitorFeedBoxV1=true;</script></body>', 1)
    dashboard.DASHBOARD_HTML = html
