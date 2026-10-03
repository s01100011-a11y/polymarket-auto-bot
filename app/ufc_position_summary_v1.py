from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import dashboard_ufc_v2 as base
from app import ufc_sh01_dashboard as ufc

app = base.app
dashboard = base.dashboard
remote = ufc.remote


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


@app.get(
    "/api/dashboard/ufc-pending-positions",
    dependencies=[Depends(dashboard._auth)],
)
def ufc_pending_positions() -> dict[str, Any]:
    try:
        remote._expire_stale_buys_persisted()
    except Exception:
        pass
    try:
        queue = remote._queue_load()
    except Exception:
        queue = {}

    rows: list[dict[str, Any]] = []
    for request_id, rec in (queue or {}).items():
        if not isinstance(rec, dict) or str(rec.get("action") or "").upper() != "BUY":
            continue
        status = str(rec.get("status") or "").upper()
        if status not in {"PENDING", "LEASED", "WAITING_APPROVAL"}:
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if str(payload.get("strategy_sport") or "").upper() != "UFC":
            continue
        if str(payload.get("strategy_source") or "").upper() != "SH01":
            continue

        pick_id = str(payload.get("strategy_pick_id") or "")
        fight_id = ""
        if pick_id.startswith("ufc332:"):
            parts = pick_id.split(":", 2)
            if len(parts) >= 2:
                fight_id = parts[1]

        rows.append(
            {
                "request_id": str(request_id),
                "trade_id": str(payload.get("trade_id") or ""),
                "status": status,
                "fight_id": fight_id,
                "strategy_pick_id": pick_id,
                "selection": str(
                    payload.get("strategy_execution_selection")
                    or payload.get("strategy_selection")
                    or payload.get("outcome")
                    or "UFC position"
                ),
                "outcome": str(payload.get("outcome") or ""),
                "market": str(payload.get("market") or ""),
                "event_title": str(payload.get("event_title") or ""),
                "units": payload.get("strategy_units"),
                "unit_usdc": payload.get("strategy_unit_usdc"),
                "target_profit_usdc": payload.get("strategy_target_profit_usdc"),
                "stake_usdc": payload.get("budget_usdc"),
                "entry_price": payload.get("max_price") or payload.get("signal_buy_price"),
                "approval_reason": payload.get("approval_reason"),
                "created_at": rec.get("created_at"),
            }
        )

    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    return {"ok": True, "rows": rows}


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
#ufcAutoTradingPanel .ufc-live-position-row.pending{background:#3b3300;color:#fff3a3}
#ufcAutoTradingPanel .ufc-live-position-row.pending b{color:#fff}
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
