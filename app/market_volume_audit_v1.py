from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import Depends, HTTPException, Query

from app import ufc_audit_data_v1 as data

app = data.app
dashboard = data.dashboard
core = data.core

def _event_slug_from_url(market_url: str) -> str:
    try:
        path = urlparse(str(market_url or "")).path.rstrip("/")
    except Exception:
        return ""
    return path.rsplit("/", 1)[-1].strip()


def _token_ids(market: dict[str, Any]) -> list[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            return [raw]
    return []


def _market_question(market: dict[str, Any]) -> str:
    return str(market.get("question") or market.get("title") or market.get("slug") or "").strip()


def _market_score(market: dict[str, Any], hint: str) -> float:
    q = _market_question(market)
    nq = re.sub(r"[^a-z0-9 ]+", " ", q.casefold())
    nh = re.sub(r"[^a-z0-9 ]+", " ", str(hint or "").casefold())
    if not nq or not nh:
        return 0.0
    if nq.strip() == nh.strip():
        return 1000.0
    if nq.strip() in nh:
        return 500.0 + len(nq)
    qtokens = {x for x in nq.split() if len(x) > 2}
    htokens = {x for x in nh.split() if len(x) > 2}
    if not qtokens:
        return 0.0
    return 100.0 * len(qtokens & htokens) / len(qtokens)


def _gamma_volume_snapshot(*, market_url: str, market_hint: str = "", asset_id: str = "", sport: str = "") -> dict[str, Any] | None:
    slug = _event_slug_from_url(market_url)
    if not slug:
        return None
    url = f"https://gamma-api.polymarket.com/events/slug/{quote(slug, safe='')}"
    with httpx.Client(timeout=8.0) as client:
        response = client.get(url)
        response.raise_for_status()
        event = response.json()
    if not isinstance(event, dict):
        return None
    markets = [row for row in (event.get("markets") or []) if isinstance(row, dict)]
    if not markets:
        return None

    chosen = None
    asset_id = str(asset_id or "").strip()
    if asset_id:
        hits = [m for m in markets if asset_id in _token_ids(m)]
        if len(hits) == 1:
            chosen = hits[0]
    if chosen is None and market_hint:
        ranked = sorted(((_market_score(m, market_hint), m) for m in markets), key=lambda x: x[0], reverse=True)
        if ranked and ranked[0][0] >= 70 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            chosen = ranked[0][1]
    if chosen is None and len(markets) == 1:
        chosen = markets[0]
    if chosen is None:
        return None

    question = _market_question(chosen)
    market_slug = str(chosen.get("slug") or "").strip()
    volume = chosen.get("volumeNum")
    if volume in (None, ""):
        volume = chosen.get("volume")
    volume_24h = chosen.get("volume24hr")
    if volume_24h in (None, ""):
        volume_24h = chosen.get("volume24hrNum")
    liquidity = chosen.get("liquidityNum")
    if liquidity in (None, ""):
        liquidity = chosen.get("liquidity")
    return {
        "sport": str(sport or "").upper(),
        "event_slug": slug,
        "event_title": event.get("title"),
        "market_slug": market_slug,
        "market": question,
        "asset_id": asset_id or None,
        "volume_usdc": volume,
        "volume_24h_usdc": volume_24h,
        "liquidity_usdc": liquidity,
        "gamma_market_id": chosen.get("id"),
        "observed_at": data._now_iso(),
    }


def _volume_record(snapshot: dict[str, Any], market_url: str) -> dict[str, Any]:
    identity = snapshot.get("market_slug") or snapshot.get("gamma_market_id") or snapshot.get("market")
    rid = data._stable_id("market-volume", snapshot.get("event_slug"), identity)
    return {
        "id": rid,
        "entity_key": f"market-volume:{snapshot.get('event_slug')}:{data._compact(identity)}",
        "sport": snapshot.get("sport") or "",
        "event": snapshot.get("event_slug") or "",
        "event_id": snapshot.get("event_slug") or "",
        "matchup": snapshot.get("event_title") or "",
        "subject": snapshot.get("market") or "",
        "data_type": "market_volume",
        "market": snapshot.get("market") or "",
        "value": snapshot.get("volume_usdc"),
        "data": {
            "volume_usdc": snapshot.get("volume_usdc"),
            "volume_24h_usdc": snapshot.get("volume_24h_usdc"),
            "liquidity_usdc": snapshot.get("liquidity_usdc"),
            "market_slug": snapshot.get("market_slug"),
            "gamma_market_id": snapshot.get("gamma_market_id"),
            "market_url": market_url,
            "asset_id": snapshot.get("asset_id"),
        },
        "source": "Polymarket Gamma via Audit DB",
        "source_url": market_url,
        "observed_at": snapshot.get("observed_at") or data._now_iso(),
        "retrieved_at": data._now_iso(),
        "confidence": "verified",
        "freshness_seconds": data._VOLUME_MAX_AGE_SECONDS,
        "tags": ["market-volume", "polymarket"],
    }


def _volume_row_matches(row: dict[str, Any], market_hint: str, asset_id: str) -> bool:
    row_data = row.get("data") if isinstance(row.get("data"), dict) else {}
    if asset_id and str(row_data.get("asset_id") or "") == str(asset_id):
        return True
    if market_hint:
        q = str(row.get("market") or row.get("subject") or "")
        return _market_score({"question": q}, market_hint) >= 70
    return True


def _latest_volume_from_audit(slug: str, market_hint: str, asset_id: str) -> dict[str, Any] | None:
    rows = data._audit_get(data_type="market_volume", event=slug, limit=100)
    for row in rows:
        if _volume_row_matches(row, market_hint, asset_id):
            return row
    return None


def _volume_stale(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return True
    dt = data._parse_dt(row.get("observed_at") or row.get("retrieved_at"))
    if dt is None:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() > data._VOLUME_MAX_AGE_SECONDS


def _normalize_volume_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {"available": False, "source": "Audit DB"}
    row_data = row.get("data") if isinstance(row.get("data"), dict) else {}
    return {
        "available": True,
        "source": "Audit DB",
        "source_detail": row.get("source"),
        "market": row.get("market") or row.get("subject"),
        "volume_usdc": row_data.get("volume_usdc") if row_data.get("volume_usdc") not in (None, "") else row.get("value"),
        "volume_24h_usdc": row_data.get("volume_24h_usdc"),
        "liquidity_usdc": row_data.get("liquidity_usdc"),
        "observed_at": row.get("observed_at") or row.get("retrieved_at"),
        "record_id": row.get("id"),
    }


def _market_volume_payload(*, market_url: str, market_hint: str = "", asset_id: str = "", sport: str = "") -> dict[str, Any]:
    slug = _event_slug_from_url(market_url)
    if not slug:
        return {"available": False, "source": "Audit DB", "error": "market_url has no event slug"}
    if not data._audit_ready():
        return {"available": False, "source": "Audit DB", "error": "Audit DB connection is not configured"}

    try:
        row = _latest_volume_from_audit(slug, market_hint, asset_id)
    except Exception as exc:
        return {"available": False, "source": "Audit DB", "error": f"Audit DB read failed: {exc}"}

    if _volume_stale(row):
        try:
            snapshot = _gamma_volume_snapshot(
                market_url=market_url,
                market_hint=market_hint,
                asset_id=asset_id,
                sport=sport,
            )
            if snapshot is not None:
                data._audit_post(_volume_record(snapshot, market_url))
                row = _latest_volume_from_audit(slug, snapshot.get("market") or market_hint, asset_id)
        except Exception as exc:
            if row is None:
                return {"available": False, "source": "Audit DB", "error": f"Volume refresh failed: {exc}"}

    return _normalize_volume_row(row)


@app.get("/api/dashboard/market-volume", dependencies=[Depends(dashboard._auth)])
def dashboard_market_volume(
    market_url: str = Query(min_length=10, max_length=1000),
    market_hint: str = Query(default="", max_length=1000),
    asset_id: str = Query(default="", max_length=256),
    sport: str = Query(default="", max_length=32),
) -> dict[str, Any]:
    return _market_volume_payload(
        market_url=market_url,
        market_hint=market_hint,
        asset_id=asset_id,
        sport=sport,
    )


@app.get("/api/dashboard/market-volume-trade/{trade_id}", dependencies=[Depends(dashboard._auth)])
def dashboard_market_volume_trade(trade_id: str) -> dict[str, Any]:
    try:
        raw = core._load(core.EXECUTIONS_FILE)
    except Exception:
        raw = {}
    rec = raw.get(trade_id) if isinstance(raw, dict) else None
    if not isinstance(rec, dict):
        raise HTTPException(status_code=404, detail="trade not found")
    quote_row = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    market_url = str(rec.get("market_url") or quote_row.get("market_url") or "").strip()
    if not market_url:
        return {"available": False, "source": "Audit DB", "error": "trade has no Polymarket market URL"}
    return _market_volume_payload(
        market_url=market_url,
        market_hint=str(rec.get("strategy_exact_position") or rec.get("exact_position") or quote_row.get("market") or rec.get("strategy_selection") or ""),
        asset_id=str(quote_row.get("asset_id") or rec.get("asset_id") or ""),
        sport=str(rec.get("strategy_sport") or rec.get("sport") or ""),
    )
