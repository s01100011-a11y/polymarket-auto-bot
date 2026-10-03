from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import dashboard_ufc_v2 as base

app = base.app
dashboard = base.dashboard


@app.get(
    "/api/dashboard/ufc-position-summary.js",
    response_class=PlainTextResponse,
    dependencies=[Depends(dashboard._auth)],
)
def ufc_position_summary_js() -> PlainTextResponse:
    path = Path(__file__).with_name("ufc_position_summary_v1.js")
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


html = dashboard.DASHBOARD_HTML
if "ufc-position-summary-v1" not in html:
    css = r'''
/* ufc-position-summary-v1 */
#ufcAutoTradingPanel .ufc-position-summary{
 display:flex;gap:8px;flex-wrap:wrap;align-items:center;
 margin:6px 0 8px;padding:6px 7px;border:2px inset #fff;background:#c0c0c0;
 color:#000;font-size:10px;font-weight:900
}
#ufcAutoTradingPanel .ufc-position-summary .positive{color:#006000}
#ufcAutoTradingPanel .ufc-position-summary .negative{color:#8b0000}
#ufcAutoTradingPanel .ufc-unit-value{
 margin:3px 0 5px;padding:3px 5px;background:#d8d8d8;border:1px inset #fff;
 color:#000;font-size:10px;font-weight:900
}
#ufcAutoTradingPanel .ufc-live-positions{margin-top:5px}
#ufcAutoTradingPanel .ufc-live-position-row{
 display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 padding:4px 5px;margin:2px 0;background:#050505;color:#fff;border:1px inset #fff;
 font:10px/1.35 "Lucida Console","Courier New",monospace
}
#ufcAutoTradingPanel .ufc-live-position-row .positive{color:#00ff66}
#ufcAutoTradingPanel .ufc-live-position-row .negative{color:#ff4040}
#ufcAutoTradingPanel .ufc-live-position-row .flat{color:#fff}
@media(max-width:700px){
 #ufcAutoTradingPanel .ufc-live-position-row{white-space:normal}
}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace(
        "</body>",
        '<script id="ufc-position-summary-v1" src="/api/dashboard/ufc-position-summary.js"></script></body>',
        1,
    )
    dashboard.DASHBOARD_HTML = html
