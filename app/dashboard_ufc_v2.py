from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import dashboard_taskbar_v3 as taskbar
from app import market_volume_audit_v1 as _market_volume  # noqa: F401
from app import ufc_audit_data_v1 as _ufc_data  # noqa: F401

dashboard = taskbar.dashboard
app = _ufc_data.app


@app.get("/api/dashboard/ufc-v2.js", response_class=PlainTextResponse, dependencies=[Depends(dashboard._auth)])
def ufc_v2_javascript() -> PlainTextResponse:
    path = Path(__file__).with_name("ufc_v2_frontend.js")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-store"})


html = dashboard.DASHBOARD_HTML
if "ufc-dashboard-v2" not in html:
    css = r'''
/* ufc-dashboard-v2 */
#ufcAutoTradingPanel{color:#000!important}
#ufcAutoTradingPanel .nfl-capper-state,#ufcAutoTradingPanel .ufc-state,#ufcAutoTradingPanel .capper-status-label{color:#000!important}
#ufcAutoTradingPanel .ufc-v2-source{font-size:10px;font-weight:800;color:#000;margin-top:3px}
#ufcAutoTradingPanel .ufc-v2-sizing{margin:7px 0 10px;padding:7px;border:2px inset #fff;background:#c0c0c0;color:#000}
#ufcAutoTradingPanel .capper-sizing-row{color:#000}
#ufcAutoTradingPanel .ufc-splits{background:#e6e6e6;color:#000;border:1px inset #fff;padding:5px 6px;margin:0 0 6px;font-size:10px;line-height:1.35}
#ufcAutoTradingPanel .ufc-splits b{font-size:10px}
#ufcAutoTradingPanel .ufc-splits.unavailable{color:#444}
#ufcAutoTradingPanel .ufc-inline-position{background:#000;color:#fff;border:1px inset #fff;margin-top:6px;padding:6px;font:10px/1.4 "Lucida Console","Courier New",monospace}
#ufcAutoTradingPanel .ufc-inline-position .positive{color:#00ff66}#ufcAutoTradingPanel .ufc-inline-position .negative{color:#ff4040}
#ufcAutoTradingPanel .ufc-market-meta{font-size:10px;color:#aaa;margin-top:5px}
.market-volume-v1{font-weight:900;white-space:nowrap}.market-volume-v1.loading{opacity:.65}.market-volume-v1.error{opacity:.65;font-weight:700}
.s01807-taskbar .ufc-task-btn svg{width:16px;height:16px}.s01807-taskbar-row.ufc-five{grid-template-columns:repeat(5,minmax(0,1fr))}
@media(max-width:700px){.s01807-taskbar-row.ufc-five button{font-size:9.5px;padding-left:2px;padding-right:2px}}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace("</body>", '<script id="ufc-dashboard-v2" src="/api/dashboard/ufc-v2.js"></script></body>', 1)
    dashboard.DASHBOARD_HTML = html
