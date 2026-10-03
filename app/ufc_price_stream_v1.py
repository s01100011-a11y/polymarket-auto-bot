from __future__ import annotations

import asyncio
import json
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi.responses import PlainTextResponse

from app import dashboard_ufc_v2 as base
from app import ufc_audit_data_v1 as audit
from app import ufc_sh01_dashboard as ufc

app = base.app
dashboard = base.dashboard

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_LOCK = threading.RLock()
_MAPPING: dict[str, dict[str, Any]] = {}
_PRICES: dict[str, dict[str, Any]] = {}
_STATUS: dict[str, Any] = {"connected": False, "last_event_unix": None, "last_error": None}


def _build_mapping() -> dict[str, dict[str, Any]]:
    card = audit._ensure_card_record()
    specs = audit._fight_specs(card, "main_card") + audit._fight_specs(card, "prelims")
    events = ufc._load_ufc_events()
    result: dict[str, dict[str, Any]] = {}
    for fight_id, fighter_a, fighter_b in specs:
        event = ufc._find_event(events, fighter_a, fighter_b)
        if event is None:
            continue
        matched = ufc._moneyline_market(event, fighter_a, fighter_b)
        if matched is None:
            continue
        _market, outcomes = matched
        for side, fighter in enumerate((fighter_a, fighter_b)):
            _label, outcome = outcomes[fighter]
            asset_id = ufc._text(getattr(outcome, "token_id", None) or getattr(outcome, "position_id", None))
            if not asset_id:
                continue
            result[asset_id] = {"fight_id": fight_id, "side": side, "fighter": fighter}
    with _LOCK:
        _MAPPING.clear()
        _MAPPING.update(result)
    return result


def _save(asset_id: str, ask: Any, bid: Any = None, source: str = "websocket") -> None:
    try:
        ask_d = Decimal(str(ask))
    except Exception:
        return
    if ask_d <= 0 or ask_d >= 1:
        return
    try:
        bid_d = Decimal(str(bid)) if bid not in {None, ""} else None
    except Exception:
        bid_d = None
    with _LOCK:
        _PRICES[asset_id] = {
            "best_ask": ask_d,
            "best_bid": bid_d,
            "received_unix": time.time(),
            "source": source,
        }
        _STATUS["last_event_unix"] = time.time()


def _handle(item: Any) -> None:
    if not isinstance(item, dict):
        return
    event_type = str(item.get("event_type") or item.get("type") or "")
    if event_type == "book":
        asset = str(item.get("asset_id") or item.get("token_id") or "")
        asks = item.get("asks") or []
        bids = item.get("bids") or []
        try:
            ask = min(Decimal(str(x.get("price"))) for x in asks if isinstance(x, dict))
        except Exception:
            ask = None
        try:
            bid = max(Decimal(str(x.get("price"))) for x in bids if isinstance(x, dict))
        except Exception:
            bid = None
        if asset and ask is not None:
            _save(asset, ask, bid, "websocket_book")
    elif event_type == "price_change":
        for ch in item.get("price_changes") or item.get("priceChanges") or []:
            if not isinstance(ch, dict):
                continue
            asset = str(ch.get("asset_id") or ch.get("token_id") or ch.get("tokenId") or "")
            ask = ch.get("best_ask") if "best_ask" in ch else ch.get("bestAsk")
            bid = ch.get("best_bid") if "best_bid" in ch else ch.get("bestBid")
            if asset and ask not in {None, ""}:
                _save(asset, ask, bid, "websocket_price_change")
    elif event_type == "best_bid_ask":
        asset = str(item.get("asset_id") or item.get("token_id") or item.get("tokenId") or "")
        ask = item.get("best_ask") if "best_ask" in item else item.get("bestAsk")
        bid = item.get("best_bid") if "best_bid" in item else item.get("bestBid")
        if asset and ask not in {None, ""}:
            _save(asset, ask, bid, "websocket_best_bid_ask")


async def _run() -> None:
    while True:
        try:
            mapping = await asyncio.to_thread(_build_mapping)
            assets = sorted(mapping)
            if not assets:
                raise RuntimeError("No UFC moneyline assets mapped")
            from websockets.asyncio.client import connect
            async with connect(_WS_URL, ping_interval=None, close_timeout=2, max_size=4_000_000) as ws:
                await ws.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True}))
                with _LOCK:
                    _STATUS["connected"] = True
                    _STATUS["last_error"] = None

                async def ping() -> None:
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")

                pinger = asyncio.create_task(ping())
                try:
                    async for raw in ws:
                        if raw in {"PING", "PONG"}:
                            continue
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(payload, list):
                            for entry in payload:
                                _handle(entry)
                        else:
                            _handle(payload)
                finally:
                    pinger.cancel()
        except Exception as exc:
            with _LOCK:
                _STATUS["connected"] = False
                _STATUS["last_error"] = f"{type(exc).__name__}: {exc}"[:400]
            await asyncio.sleep(1)


def _thread_main() -> None:
    asyncio.run(_run())


@app.get("/api/dashboard/ufc-stream-prices", dependencies=[Depends(dashboard._auth)])
def ufc_stream_prices() -> dict[str, Any]:
    now = time.time()
    with _LOCK:
        mapping = dict(_MAPPING)
        prices = {k: dict(v) for k, v in _PRICES.items()}
        status = dict(_STATUS)
    rows = []
    for asset, meta in mapping.items():
        p = prices.get(asset) or {}
        ask = p.get("best_ask")
        odds = None
        if ask is not None:
            try:
                odds = (Decimal("1") / Decimal(str(ask))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except Exception:
                odds = None
        rows.append({
            **meta,
            "asset_id": asset,
            "best_ask": str(ask) if ask is not None else None,
            "decimal_odds": str(odds) if odds is not None else None,
            "age_ms": int(max(0, now - float(p.get("received_unix") or now)) * 1000) if p else None,
            "source": p.get("source"),
        })
    return {"ok": True, "rows": rows, "stream": status}


@app.get("/api/dashboard/ufc-price-stream.js", response_class=PlainTextResponse, dependencies=[Depends(dashboard._auth)])
def ufc_price_stream_js() -> PlainTextResponse:
    path = Path(__file__).with_name("ufc_price_stream_v1.js")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/javascript", headers={"Cache-Control": "no-store"})


html = dashboard.DASHBOARD_HTML
if "ufc-price-stream-v1" not in html:
    css = r'''
/* ufc-price-stream-v1 */
#ufcAutoTradingPanel .ufc-stream-state{font-size:10px;font-weight:900;margin:4px 0;color:#006000}
#ufcAutoTradingPanel .ufc-stream-state.off{color:#800000}
#ufcAutoTradingPanel .ufc-ml-buttons button.stream-fresh{outline:2px solid #008000;outline-offset:-2px}
'''
    html = html.replace("</style>", css + "</style>", 1)
    html = html.replace("</body>", '<script id="ufc-price-stream-v1" src="/api/dashboard/ufc-price-stream.js"></script></body>', 1)
    dashboard.DASHBOARD_HTML = html

threading.Thread(target=_thread_main, name="ufc-price-stream", daemon=True).start()
