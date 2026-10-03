from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Query

from app import dashboard_taskbar_v3 as taskbar
from app import nfl_capper_ingest as nfl
from app import performance_sizing_v1 as performance
from app import ufc_sh01_dashboard as ufc

app = ufc.app
dashboard = taskbar.dashboard
core = ufc.core

_AUDIT_URL = os.getenv("AUDIT_CORE_URL", "").strip().rstrip("/")
_AUDIT_TOKEN = os.getenv("AUDIT_CORE_TOKEN", "").strip()
_UFC_EVENT = "UFC 332"
_UFC_EVENT_ID = "ufc-332-2026-10-03"
_VOLUME_MAX_AGE_SECONDS = max(15, int(os.getenv("MARKET_VOLUME_AUDIT_MAX_AGE_SECONDS", "60")))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _audit_headers() -> dict[str, str]:
    return {"X-Audit-Core-Token": _AUDIT_TOKEN}


def _audit_ready() -> bool:
    return bool(_AUDIT_URL and _AUDIT_TOKEN)


def _audit_get(*, sport: str = "", data_type: str = "", event: str = "", limit: int = 200) -> list[dict[str, Any]]:
    if not _audit_ready():
        raise RuntimeError("Audit DB connection is not configured")
    with httpx.Client(timeout=8.0) as client:
        response = client.get(
            f"{_AUDIT_URL}/api/core/research",
            headers=_audit_headers(),
            params={
                "sport": sport,
                "data_type": data_type,
                "event": event,
                "limit": max(1, min(int(limit), 1000)),
                "latest_only": "true",
            },
        )
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("records") if isinstance(payload, dict) else []
    return [row for row in (rows or []) if isinstance(row, dict)]


def _audit_post(record: dict[str, Any]) -> dict[str, Any]:
    if not _audit_ready():
        raise RuntimeError("Audit DB connection is not configured")
    with httpx.Client(timeout=8.0) as client:
        response = client.post(
            f"{_AUDIT_URL}/api/core/research",
            headers=_audit_headers(),
            json=record,
        )
        response.raise_for_status()
        return response.json() if response.content else {"ok": True}


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(x or "") for x in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _card_seed() -> dict[str, Any]:
    main = [
        {"fight_id": fight_id, "fighter_a": a, "fighter_b": b}
        for fight_id, a, b in ufc.MAIN_CARD
    ]
    prelims = [
        {"fight_id": fight_id, "fighter_a": a, "fighter_b": b}
        for fight_id, a, b in ufc.PRELIMS
    ]
    return {
        "id": "ufc-332-event-card-v1",
        "entity_key": "ufc:ufc-332:event_card:full_card",
        "sport": "UFC",
        "league": "UFC",
        "event": _UFC_EVENT,
        "event_id": _UFC_EVENT_ID,
        "matchup": "UFC 332: Silva vs Wang",
        "subject": "full card",
        "data_type": "event_card",
        "market": "moneyline",
        "value": {
            "event_name": "UFC 332: Silva vs Wang",
            "event_us_date": "2026-10-03",
            "event_malaysia_date": "2026-10-04",
        },
        "data": {
            "main_card": main,
            "prelims": prelims,
            "capper": "SH01",
        },
        "source": "Audit DB UFC 332 verified-card seed",
        "observed_at": "2026-10-03T10:00:00+00:00",
        "retrieved_at": _now_iso(),
        "confidence": "verified",
        "notes": "Dashboard must read UFC informational metadata from Audit DB. Live executable price/order-book validation remains direct to Polymarket.",
        "tags": ["ufc332", "card", "main-card", "prelims"],
    }


def _ensure_card_record() -> dict[str, Any]:
    rows = _audit_get(sport="UFC", data_type="event_card", event=_UFC_EVENT, limit=20)
    if rows:
        return rows[0]
    _audit_post(_card_seed())
    rows = _audit_get(sport="UFC", data_type="event_card", event=_UFC_EVENT, limit=20)
    if not rows:
        raise RuntimeError("Audit DB did not return the UFC 332 card after seed")
    return rows[0]


def _fight_specs(record: dict[str, Any], key: str) -> list[tuple[str, str, str]]:
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    rows = data.get(key) if isinstance(data.get(key), list) else []
    out: list[tuple[str, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fight_id = str(row.get("fight_id") or "").strip()
        fighter_a = str(row.get("fighter_a") or "").strip()
        fighter_b = str(row.get("fighter_b") or "").strip()
        if fight_id and fighter_a and fighter_b:
            out.append((fight_id, fighter_a, fighter_b))
    return out


def _split_match(row: dict[str, Any], fighter_a: str, fighter_b: str) -> bool:
    hay = " ".join(
        str(row.get(key) or "")
        for key in ("event", "matchup", "subject", "market", "notes")
    )
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    hay += " " + json.dumps(data, ensure_ascii=False, default=str)
    c = _compact(hay)
    for fighter in (fighter_a, fighter_b):
        full = _compact(fighter)
        last = _compact(str(fighter).split()[-1])
        if full not in c and (len(last) < 4 or last not in c):
            return False
    return True


def _pct(value: Any) -> str | None:
    try:
        n = Decimal(str(value))
    except Exception:
        return None
    if abs(n) <= 1:
        n *= 100
    if n < 0 or n > 100:
        return None
    return f"{n.quantize(Decimal('0.1')).normalize()}%"


def _fighter_split_from_obj(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    bets = None
    handle = None
    odds = None
    for key, value in obj.items():
        nk = _compact(key)
        if bets is None and any(x in nk for x in ("betspct", "ticketspct", "betpct", "tickets")):
            bets = _pct(value)
        if handle is None and any(x in nk for x in ("handlepct", "moneypct", "handle", "money")):
            handle = _pct(value)
        if odds is None and nk in {"odds", "moneyline", "ml"}:
            odds = str(value)
    if not any((bets, handle, odds)):
        return None
    return {"bets_pct": bets, "handle_pct": handle, "odds": odds}


def _normalize_split(row: dict[str, Any] | None, fighter_a: str, fighter_b: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    pools: list[dict[str, Any]] = [data]
    for key in ("moneyline", "ml", "fighters", "sides", "split", "splits"):
        val = data.get(key)
        if isinstance(val, dict):
            pools.append(val)

    fighters: dict[str, Any] = {}
    for fighter in (fighter_a, fighter_b):
        aliases = {_compact(fighter), _compact(str(fighter).split()[-1])}
        found = None
        for pool in pools:
            for key, value in pool.items():
                if _compact(key) in aliases and isinstance(value, dict):
                    found = _fighter_split_from_obj(value)
                    if found:
                        break
            if found:
                break
        if found:
            fighters[fighter] = found

    if len(fighters) < 2:
        flat = {str(k): v for pool in pools for k, v in pool.items() if not isinstance(v, dict)}
        for idx, fighter in enumerate((fighter_a, fighter_b)):
            if fighter in fighters:
                continue
            side_names = {"a" if idx == 0 else "b", "fightera" if idx == 0 else "fighterb", _compact(fighter), _compact(fighter.split()[-1])}
            obj: dict[str, Any] = {}
            for key, value in flat.items():
                nk = _compact(key)
                if not any(side in nk for side in side_names):
                    continue
                if "bet" in nk or "ticket" in nk:
                    obj["bets_pct"] = value
                if "handle" in nk or "money" in nk:
                    obj["handle_pct"] = value
                if nk.endswith("odds") or nk.endswith("moneyline") or nk.endswith("ml"):
                    obj["odds"] = value
            parsed = _fighter_split_from_obj(obj)
            if parsed:
                fighters[fighter] = parsed

    return {
        "available": bool(fighters),
        "source": row.get("source") or "Audit DB",
        "observed_at": row.get("observed_at") or row.get("retrieved_at"),
        "fighters": fighters,
        "raw_data_type": row.get("data_type"),
    }


def _split_rows() -> list[dict[str, Any]]:
    try:
        return _audit_get(sport="UFC", data_type="betting_splits", event=_UFC_EVENT, limit=200)
    except Exception:
        return []


def _split_for_fight(rows: list[dict[str, Any]], fighter_a: str, fighter_b: str) -> dict[str, Any] | None:
    for row in rows:
        if _split_match(row, fighter_a, fighter_b):
            return _normalize_split(row, fighter_a, fighter_b)
    return None


def _card_payload_from_audit() -> dict[str, Any]:
    load_error = None
    audit_error = None
    try:
        card_record = _ensure_card_record()
        main_specs = _fight_specs(card_record, "main_card")
        prelim_specs = _fight_specs(card_record, "prelims")
    except Exception as exc:
        card_record = {}
        main_specs = []
        prelim_specs = []
        audit_error = f"{type(exc).__name__}: {exc}"

    splits = _split_rows() if main_specs or prelim_specs else []
    try:
        events = ufc._load_ufc_events() if (main_specs or prelim_specs) else []
    except Exception as exc:
        events = []
        load_error = f"{type(exc).__name__}: {exc}"

    def build(specs: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for fight_id, fighter_a, fighter_b in specs:
            row = ufc._fight_payload(events, fight_id, fighter_a, fighter_b)
            row["betting_splits"] = _split_for_fight(splits, fighter_a, fighter_b)
            row["info_source"] = "Audit DB"
            rows.append(row)
        return rows

    main = build(main_specs)
    prelims = build(prelim_specs)
    unit_config = nfl._capper_unit_config(core, ufc.CAPPER_LABEL, ufc.DEFAULT_UNIT_USDC)
    all_rows = main + prelims
    market_count = sum(1 for row in all_rows if row.get("market_found"))
    return {
        "event": (card_record.get("value") or {}).get("event_name") if isinstance(card_record.get("value"), dict) else "UFC 332: Silva vs Wang",
        "event_local_date": (card_record.get("value") or {}).get("event_malaysia_date") if isinstance(card_record.get("value"), dict) else None,
        "capper": "SH01",
        "sport": "UFC",
        "scope": "tomorrow_only",
        "info_source": "Audit DB",
        "audit_record_id": card_record.get("id") if isinstance(card_record, dict) else None,
        "updated_at": _now_iso(),
        "market_count": market_count,
        "fight_count": len(all_rows),
        "load_error": load_error,
        "audit_error": audit_error,
        "unit": unit_config,
        "main_card": main,
        "prelims": prelims,
    }


ufc._card_payload = _card_payload_from_audit


def _resolve_live_fighter_from_audit(fight_id: str, side: int) -> dict[str, Any]:
    if side not in {0, 1}:
        raise HTTPException(status_code=400, detail="Unknown fighter side")
    try:
        card = _ensure_card_record()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Audit DB UFC card unavailable: {exc}") from exc
    specs = _fight_specs(card, "main_card") + _fight_specs(card, "prelims")
    chosen = next((row for row in specs if row[0] == fight_id), None)
    if chosen is None:
        raise HTTPException(status_code=404, detail="Unknown UFC 332 fight in Audit DB")
    _, fighter_a, fighter_b = chosen
    fighter = (fighter_a, fighter_b)[side]
    try:
        events = ufc._load_ufc_events()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not refresh UFC execution markets: {exc}") from exc
    event = ufc._find_event(events, fighter_a, fighter_b)
    if event is None:
        raise HTTPException(status_code=409, detail="The selected UFC fight has no unique open Polymarket event")
    matched = ufc._moneyline_market(event, fighter_a, fighter_b)
    if matched is None:
        raise HTTPException(status_code=409, detail="The selected UFC fight has no exact two-fighter moneyline market")
    market, mapping = matched
    label, outcome = mapping[fighter]
    asset_id = ufc._text(getattr(outcome, "token_id", None) or getattr(outcome, "position_id", None))
    if not asset_id:
        raise HTTPException(status_code=409, detail="The selected fighter moneyline has no tradable token")
    event_slug = ufc._text(getattr(event, "slug", ""))
    if not event_slug.casefold().startswith("ufc-"):
        raise HTTPException(status_code=409, detail="Resolved event is not a UFC Polymarket event")
    return {
        "fighter": fighter,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "outcome": label,
        "asset_id": asset_id,
        "event_slug": event_slug,
        "event_title": ufc._text(getattr(event, "title", "")),
        "market": ufc._text(getattr(market, "question", "") or getattr(event, "title", "")),
        "market_url": f"https://polymarket.com/sports/ufc/{event_slug}",
    }


ufc._resolve_live_fighter = _resolve_live_fighter_from_audit


_original_excluded = performance._excluded


def _performance_excluded(label: str) -> bool:
    if performance._norm(label) == "sh01ufc":
        return False
    return _original_excluded(label)


performance._excluded = _performance_excluded


@app.get("/api/dashboard/ufc-sh01-sizing", dependencies=[Depends(dashboard._auth)])
def ufc_sh01_sizing() -> dict[str, Any]:
    return {
        "label": ufc.CAPPER_LABEL,
        **nfl._capper_unit_config(core, ufc.CAPPER_LABEL, ufc.DEFAULT_UNIT_USDC),
    }


@app.put("/api/dashboard/ufc-sh01-sizing", dependencies=[Depends(dashboard._auth)])
def ufc_sh01_set_sizing(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode") or "").strip().lower()
    try:
        if mode == "fixed":
            nfl._set_capper_unit_usdc(core, ufc.CAPPER_LABEL, payload.get("value"))
        elif mode == "portfolio_pct":
            nfl._set_capper_portfolio_pct(core, ufc.CAPPER_LABEL, payload.get("value"))
        elif mode == "performance":
            setter = getattr(nfl, "_set_capper_performance_mode", None)
            if not callable(setter):
                raise ValueError("Performance staking is not installed")
            setter(core, ufc.CAPPER_LABEL)
        else:
            raise ValueError("mode must be fixed, portfolio_pct, or performance")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "label": ufc.CAPPER_LABEL,
        **nfl._capper_unit_config(core, ufc.CAPPER_LABEL, ufc.DEFAULT_UNIT_USDC),
    }
