from __future__ import annotations

from typing import Any

from app import termux_executor_dashboard as remote

_ORIGINAL = remote._executor_event


def _descriptor(payload: dict[str, Any]) -> str:
    sport = str(payload.get("strategy_sport") or "").strip().upper()
    event = str(payload.get("event_title") or payload.get("market") or "").strip()
    selection = str(
        payload.get("strategy_execution_selection")
        or payload.get("strategy_selection")
        or payload.get("outcome")
        or ""
    ).strip()
    parts = []
    for value in (sport, event, selection):
        if value and value not in parts:
            parts.append(value)
    return " · ".join(parts)


def _labeled_event(rec: dict[str, Any]) -> dict[str, Any]:
    out = _ORIGINAL(rec)
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    desc = _descriptor(payload)
    if desc:
        msg = str(out.get("message") or "").strip()
        out["message"] = f"{desc} · {msg}" if msg else desc
        out["order_label"] = desc
        out["market_url"] = payload.get("market_url")
        out["selection"] = payload.get("strategy_execution_selection") or payload.get("strategy_selection") or payload.get("outcome")
        out["sport"] = payload.get("strategy_sport")
    return out


remote._executor_event = _labeled_event
