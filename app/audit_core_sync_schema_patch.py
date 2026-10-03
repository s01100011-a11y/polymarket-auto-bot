from __future__ import annotations

from typing import Any

_INSTALLED = False


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def install(sync_module: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original = sync_module._pw_signal_payload

    def enriched(event_id: str, alert: dict[str, Any]):
        payload = original(event_id, alert)
        if not isinstance(payload, dict):
            return payload
        record = alert.get("pw_export_record")
        if not isinstance(record, dict):
            return payload

        # NBAMonitor's canonical fields are deliberately preserved rather than
        # collapsed so Audit DB can show the same bookmaker evidence the PW
        # strategy saw when it fired.
        moneyline = _first(record, "bk_moneyline", "bk_ml", "bkML", "moneyline", "ml")
        spread = _first(record, "bk_spread", "bkSpread", "live_spread", "spread")
        spread_price = _first(record, "bk_spread_price", "bkSpreadPrice", "spread_price")
        total_over = _first(record, "bk_total_over", "bkTotalOver", "total_over")
        total_under = _first(record, "bk_total_under", "bkTotalUnder", "total_under")
        total_over_price = _first(record, "bk_total_over_price", "bkTotalOverPrice", "total_over_price")
        total_under_price = _first(record, "bk_total_under_price", "bkTotalUnderPrice", "total_under_price")
        totals_source = _first(record, "bk_totals_source", "bk_total_source", "odds_source", "bookmaker")

        if moneyline not in (None, ""):
            payload["bk_ml"] = moneyline
            payload["bk_ml_source"] = _first(record, "bk_ml_source", "bk_moneyline_source", "bookmaker")
        if spread not in (None, ""):
            payload["bk_spread"] = spread
            payload["bk_spread_price"] = spread_price
            payload["bk_spread_source"] = _first(record, "bk_spread_source", "bookmaker")
        if total_over not in (None, "") or total_under not in (None, ""):
            payload["bk_total_over"] = total_over
            payload["bk_total_under"] = total_under
            payload["bk_total_over_price"] = total_over_price
            payload["bk_total_under_price"] = total_under_price
            payload["bk_totals_source"] = totals_source
            # Compatibility fields consumed by the existing live-page columns.
            payload["bk_total"] = total_over if total_over not in (None, "") else total_under
            payload["live_total"] = payload["bk_total"]
            payload["bk_total_price"] = total_over_price if total_over_price not in (None, "") else total_under_price

        payload["bk_source"] = (
            payload.get("bk_ml_source")
            or payload.get("bk_spread_source")
            or totals_source
            or payload.get("bk_source")
        )
        payload["game_clock"] = _first(record, "game_clock", "clock")
        payload["score_margin_at_fire"] = _first(record, "score_margin_at_fire", "margin_at_fire")
        payload["avg_edge"] = _first(record, "avg_edge", "edge_pp", "edge")
        payload["spread_role"] = _first(record, "spread_role")
        payload["market_decisions"] = record.get("market_decisions") if isinstance(record.get("market_decisions"), dict) else {}

        markets = list(payload.get("available_markets") or [])
        if moneyline not in (None, "") and "BK ML" not in markets:
            markets.append("BK ML")
        if spread not in (None, "") and "BK SPR" not in markets:
            markets.append("BK SPR")
        if (total_over not in (None, "") or total_under not in (None, "")) and "BK TOTAL" not in markets:
            markets.append("BK TOTAL")
        payload["available_markets"] = markets
        return payload

    sync_module._pw_signal_payload = enriched
