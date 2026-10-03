from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import ufc_price_stream_v1 as base

app = base.app
dashboard = base.dashboard


@app.get("/api/dashboard/ufc-disabled-action.js", response_class=PlainTextResponse, dependencies=[Depends(dashboard._auth)])
def ufc_disabled_action_js() -> PlainTextResponse:
    path = Path(__file__).with_name("ufc_disabled_fast_action_v1.js")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-store"})


html = dashboard.DASHBOARD_HTML
if "ufc-disabled-fast-action-v1" not in html:
    css = r'''
/* ufc-disabled-fast-action-v1 */
#ufcAutoTradingPanel .ufc-disabled-fast-action{display:flex;justify-content:flex-end;margin:4px 0 6px}
#ufcAutoTradingPanel .ufc-disabled-fast-action button{font-weight:900;font-size:10px;min-height:26px;padding:3px 8px;color:#666!important;background:#c0c0c0!important;cursor:not-allowed!important;opacity:.8}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace("</body>", '<script id="ufc-disabled-fast-action-v1" src="/api/dashboard/ufc-disabled-action.js"></script></body>', 1)
    dashboard.DASHBOARD_HTML = html
