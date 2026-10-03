from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from app import ufc_price_stream_v1 as stream
from app import ufc_sh01_dashboard as ufc

_ORIGINAL_RESOLVE = ufc._resolve_live_fighter
_ORIGINAL_QUOTE = ufc._live_quote
_MAX_HOT_PRICE_AGE = 2.0


def _resolve_cached(fight_id: str, side: int) -> dict[str, Any]:
    cached = stream.cached_selection(fight_id, side)
    if cached is None:
        return _ORIGINAL_RESOLVE(fight_id, side)
    asset_id, meta, _price = cached
    required = ("fighter", "fighter_a", "fighter_b", "outcome", "event_slug", "event_title", "market", "market_url")
    if not all(meta.get(k) for k in required):
        return _ORIGINAL_RESOLVE(fight_id, side)
    if not str(meta.get("event_slug") or "").casefold().startswith("ufc-"):
        raise HTTPException(status_code=409, detail="Cached event is not a UFC Polymarket event")
    return {
        "fighter": meta["fighter"],
        "fighter_a": meta["fighter_a"],
        "fighter_b": meta["fighter_b"],
        "outcome": meta["outcome"],
        "asset_id": asset_id,
        "event_slug": meta["event_slug"],
        "event_title": meta["event_title"],
        "market": meta["market"],
        "market_url": meta["market_url"],
    }


def _hot_quote(asset_id: str) -> dict[str, Decimal]:
    now = time.time()
    with stream._LOCK:
        row = dict(stream._PRICES.get(str(asset_id)) or {})
    try:
        ask = Decimal(str(row.get("best_ask")))
        received = float(row.get("received_unix") or 0)
    except Exception:
        return _ORIGINAL_QUOTE(asset_id)
    age = now - received if received else 999999
    if ask <= 0 or ask >= 1 or age > _MAX_HOT_PRICE_AGE:
        return _ORIGINAL_QUOTE(asset_id)
    try:
        bid = Decimal(str(row.get("best_bid"))) if row.get("best_bid") not in {None, ""} else None
    except Exception:
        bid = None
    # Never invent a zero spread. If top-of-book does not include a usable bid,
    # fall back to the old direct quote path so the spread safeguard remains exact.
    if bid is None or bid <= 0 or bid >= 1 or bid > ask:
        return _ORIGINAL_QUOTE(asset_id)
    return {"buy_price": ask, "best_ask": ask, "spread": ask - bid}


# The existing BUY endpoint resolves these functions at request time. Replacing
# them here removes the expensive event rediscovery and 3-call REST quote path
# whenever the websocket cache is healthy, while retaining the old path as a
# correctness fallback.
ufc._resolve_live_fighter = _resolve_cached
ufc._live_quote = _hot_quote
