from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import dashboard_ufc_v2 as base
from app import ufc_audit_data_v1 as audit
from app import ufc_sh01_dashboard as ufc

app = base.app
dashboard = base.dashboard

_TIMING: tuple[tuple[str, str, str, bool], ...] = (
    ("court-mcgee-eric-nolan", "EARLY PRELIMS", "2026-10-03T20:00:00+00:00", True),
    ("jacobe-smith-bruce-whitehead", "EARLY PRELIMS", "2026-10-03T20:25:00+00:00", False),
    ("marvin-vettori-ismail-naurdiev", "EARLY PRELIMS", "2026-10-03T20:50:00+00:00", False),
    ("rafael-dos-anjos-alexander-hernandez", "EARLY PRELIMS", "2026-10-03T21:15:00+00:00", False),
    ("johnny-walker-mick-parkin", "EARLY PRELIMS", "2026-10-03T21:40:00+00:00", False),
    ("anthony-wint-lucas-armand", "PRELIMS", "2026-10-03T22:00:00+00:00", True),
    ("marcus-mcghee-anthony-romero", "PRELIMS", "2026-10-03T22:30:00+00:00", False),
    ("damian-pinas-andrey-pulyaev", "PRELIMS", "2026-10-03T23:00:00+00:00", False),
    ("imanol-rodriguez-alden-coria", "PRELIMS", "2026-10-03T23:30:00+00:00", False),
    ("ateba-gautier-roman-kopylov", "MAIN CARD", "2026-10-04T00:00:00+00:00", True),
    ("roberto-soldic-khaos-williams", "MAIN CARD", "2026-10-04T00:30:00+00:00", False),
    ("king-green-esteban-ribovics", "MAIN CARD", "2026-10-04T01:00:00+00:00", False),
    ("deiveson-figueiredo-payton-talbott", "MAIN CARD", "2026-10-04T01:30:00+00:00", False),
    ("natalia-silva-wang-cong", "MAIN CARD", "2026-10-04T02:00:00+00:00", False),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed() -> dict[str, Any]:
    fights = {
        fid: {
            "fight_id": fid,
            "block": block,
            "scheduled_start_at": start,
            "time_accuracy": "official_block_start" if official else "estimated_from_block_order",
            "official": official,
        }
        for fid, block, start, official in _TIMING
    }
    return {
        "id": "ufc-332-event-timing-v1",
        "entity_key": "ufc:ufc-332:event_timing:myt",
        "sport": "UFC",
        "league": "UFC",
        "event": "UFC 332",
        "matchup": "UFC 332: Silva vs Wang",
        "subject": "fight start schedule",
        "data_type": "event_timing",
        "source": "UFC official event schedule",
        "source_url": "https://www.ufc.com/news/how-watch-and-stream-ufc",
        "observed_at": _now(),
        "retrieved_at": _now(),
        "confidence": "verified",
        "value": {
            "timezone_display": "Asia/Kuala_Lumpur",
            "early_prelims_start_at": "2026-10-03T20:00:00+00:00",
            "prelims_start_at": "2026-10-03T22:00:00+00:00",
            "main_card_start_at": "2026-10-04T00:00:00+00:00",
        },
        "data": {
            "fights": fights,
            "method": "Official UFC block start times; later individual fight times estimated from published bout order and normal broadcast spacing.",
        },
        "notes": "Exact individual walkout times are not published and can move based on earlier fight duration.",
        "tags": ["ufc332", "schedule", "fight-time", "malaysia"],
    }


def _timing() -> dict[str, dict[str, Any]]:
    rows = audit._audit_get(sport="UFC", data_type="event_timing", event="UFC 332", limit=20)
    if not rows:
        audit._audit_post(_seed())
        rows = audit._audit_get(sport="UFC", data_type="event_timing", event="UFC 332", limit=20)
    if not rows:
        return {}
    data = rows[0].get("data") if isinstance(rows[0].get("data"), dict) else {}
    fights = data.get("fights") if isinstance(data.get("fights"), dict) else {}
    return {str(k): dict(v) for k, v in fights.items() if isinstance(v, dict)}


_original_card_payload = ufc._card_payload


def _card_with_timing() -> dict[str, Any]:
    payload = _original_card_payload()
    try:
        timing = _timing()
        payload["timing_source"] = "Audit DB"
    except Exception as exc:
        timing = {}
        payload["timing_source"] = "Audit DB unavailable"
        payload["timing_error"] = f"{type(exc).__name__}: {exc}"
    for row in list(payload.get("main_card") or []) + list(payload.get("prelims") or []):
        if isinstance(row, dict):
            row["timing"] = timing.get(str(row.get("fight_id") or ""))
    return payload


ufc._card_payload = _card_with_timing


@app.get("/api/dashboard/ufc-fight-timing.js", response_class=PlainTextResponse, dependencies=[Depends(dashboard._auth)])
def ufc_fight_timing_js() -> PlainTextResponse:
    path = Path(__file__).with_name("ufc_fight_timing_v1.js")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-store"})


html = dashboard.DASHBOARD_HTML
if "ufc-fight-timing-v1" not in html:
    css = r'''
/* ufc-fight-timing-v1 */
#ufcAutoTradingPanel .ufc-fight-time{background:#111;color:#fff;border:1px inset #fff;padding:5px 6px;margin:0 0 6px;font:900 10px/1.35 "Lucida Console","Courier New",monospace}
#ufcAutoTradingPanel .ufc-fight-time .countdown{color:#00ff66}
#ufcAutoTradingPanel .ufc-fight-time .estimated{color:#ffd34d}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace("</body>", '<script id="ufc-fight-timing-v1" src="/api/dashboard/ufc-fight-timing.js"></script></body>', 1)
    dashboard.DASHBOARD_HTML = html
