from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import dashboard_metrics_v3 as metrics
from app import dashboard_sh01_capper_v3 as sh01

_ORIGINAL_MORE_STATS = metrics._more_stats_payload
_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}
_BET_TYPES = ("ML", "Spread", "Total")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bet_type(item: dict[str, Any]) -> str:
    raw = str(item.get("market_type") or item.get("strategy_market_type") or "").strip().casefold()
    if raw in {"total", "totals", "ou", "o/u", "overunder", "over/under"}:
        return "Total"
    if raw in {"spread", "spreads", "handicap", "line"}:
        return "Spread"
    if raw in {"ml", "moneyline", "h2h", "winner", "matchwinner"}:
        return "ML"

    text = " ".join(
        str(item.get(k) or "")
        for k in ("selection", "market", "exact_position", "outcome")
    ).casefold()
    if "o/u" in text or "over/under" in text or " under " in f" {text} " or " over " in f" {text} ":
        return "Total"
    if "spread" in text or any(token in text for token in (" +1.5", " +2.5", " +3", " +6.5", " -1.5", " -2.5", " -3", " -6.5")):
        return "Spread"
    return "ML"


def _blank_type(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "bets": 0,
        "open": 0,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "graded": 0,
        "stake": Decimal("0"),
        "realized": Decimal("0"),
        "realized_7d": Decimal("0"),
        "realized_30d": Decimal("0"),
        "unrealized": Decimal("0"),
        "open_value": Decimal("0"),
        "units_called": Decimal("0"),
        "realized_units": Decimal("0"),
        "live_units": Decimal("0"),
    }


def _summarize_type(row: dict[str, Any]) -> dict[str, Any]:
    decided = row["wins"] + row["losses"]
    win = Decimal(row["wins"]) / Decimal(decided) * Decimal("100") if decided else None
    roi = row["realized"] / row["stake"] * Decimal("100") if row["stake"] > 0 else None
    total_live = row["realized"] + row["unrealized"]
    total_units = row["realized_units"] + row["live_units"]
    return {
        "name": row["name"],
        "bets": row["bets"],
        "open": row["open"],
        "wins": row["wins"],
        "losses": row["losses"],
        "pushes": row["pushes"],
        "graded": row["graded"],
        "win_pct": str(win.quantize(Decimal("0.1"))) if win is not None else None,
        "stake_usdc": str(row["stake"].quantize(Decimal("0.01"))),
        "realized_pnl_usdc": str(row["realized"].quantize(Decimal("0.01"))),
        "realized_pnl_7d_usdc": str(row["realized_7d"].quantize(Decimal("0.01"))),
        "realized_pnl_30d_usdc": str(row["realized_30d"].quantize(Decimal("0.01"))),
        "unrealized_pnl_usdc": str(row["unrealized"].quantize(Decimal("0.01"))),
        "total_live_pnl_usdc": str(total_live.quantize(Decimal("0.01"))),
        "open_value_usdc": str(row["open_value"].quantize(Decimal("0.01"))),
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
        "units_called": str(row["units_called"].quantize(Decimal("0.01"))),
        "realized_units_pnl": str(row["realized_units"].quantize(Decimal("0.01"))),
        "live_units_pnl": str(row["live_units"].quantize(Decimal("0.01"))),
        "total_live_units_pnl": str(total_units.quantize(Decimal("0.01"))),
    }


def _visible_sh01_summary() -> dict[str, Any]:
    payload = sh01._sh01_cappers_payload()
    sports = payload.get("sports") if isinstance(payload, dict) else {}
    if not isinstance(sports, dict):
        sports = {}

    totals = {
        "bets": 0, "open": 0, "wins": 0, "losses": 0, "pushes": 0,
        "stake": Decimal("0"), "realized": Decimal("0"),
        "realized_7d": Decimal("0"), "realized_30d": Decimal("0"),
        "unrealized": Decimal("0"), "open_value": Decimal("0"),
        "units_called": Decimal("0"), "realized_units": Decimal("0"), "live_units": Decimal("0"),
    }
    sport_stats: list[dict[str, Any]] = []
    sport_names: list[str] = []
    type_rows = {name: _blank_type(name) for name in _BET_TYPES}
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    for sport, raw_row in sports.items():
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        positions = row.get("positions") if isinstance(row.get("positions"), list) else []
        if not positions and _i(row.get("bets")) == 0:
            continue

        sport_name = str(row.get("sport") or sport or "").upper()
        if sport_name:
            sport_names.append(sport_name)
        stake = _d(row.get("stake_usdc") or row.get("graded_stake_usdc"))
        totals["bets"] += _i(row.get("bets"))
        totals["open"] += _i(row.get("open"))
        totals["wins"] += _i(row.get("wins"))
        totals["losses"] += _i(row.get("losses"))
        totals["pushes"] += _i(row.get("pushes"))
        totals["stake"] += stake
        totals["realized"] += _d(row.get("realized_pnl_usdc"))
        totals["realized_7d"] += _d(row.get("realized_pnl_7d_usdc"))
        totals["realized_30d"] += _d(row.get("realized_pnl_30d_usdc"))
        totals["unrealized"] += _d(row.get("unrealized_pnl_usdc"))
        totals["open_value"] += _d(row.get("open_value_usdc"))
        totals["units_called"] += _d(row.get("units_called"))
        totals["realized_units"] += _d(row.get("realized_units_pnl"))
        totals["live_units"] += _d(row.get("live_units_pnl"))

        row["name"] = sport_name
        row["stake_usdc"] = str(stake.quantize(Decimal("0.01")))
        row.setdefault("total_live_pnl_usdc", str((_d(row.get("realized_pnl_usdc")) + _d(row.get("unrealized_pnl_usdc"))).quantize(Decimal("0.01"))))
        sport_stats.append(row)

        for item in positions:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").upper()
            result = str(item.get("result") or "").upper()
            is_open = bool(item.get("sell_available")) or status in _ACTIVE
            is_graded = result in {"WIN", "LOSS", "PUSH"}
            if not is_open and not is_graded:
                continue
            bucket = type_rows[_bet_type(item)]
            bucket["bets"] += 1
            stake_i = _d(item.get("stake_usdc"))
            unit_usdc = _d(item.get("unit_usdc"))
            units = _d(item.get("units"))
            if units > 0:
                bucket["units_called"] += units
            if is_open:
                bucket["open"] += 1
                live = _d(item.get("live_pnl_usdc"))
                bucket["unrealized"] += live
                bucket["open_value"] += _d(item.get("current_value_usdc"))
                if unit_usdc > 0:
                    bucket["live_units"] += live / unit_usdc
                continue
            pnl = _d(item.get("realized_pnl_usdc"))
            bucket["graded"] += 1
            bucket["stake"] += stake_i
            bucket["realized"] += pnl
            if result == "WIN":
                bucket["wins"] += 1
            elif result == "LOSS":
                bucket["losses"] += 1
            else:
                bucket["pushes"] += 1
            if unit_usdc > 0:
                bucket["realized_units"] += pnl / unit_usdc
            closed = _when(item.get("closed_at") or item.get("submitted_at"))
            if closed is not None:
                if closed >= cutoff_30d:
                    bucket["realized_30d"] += pnl
                if closed >= cutoff_7d:
                    bucket["realized_7d"] += pnl

    decided = totals["wins"] + totals["losses"]
    win = Decimal(totals["wins"]) / Decimal(decided) * Decimal("100") if decided else None
    roi = totals["realized"] / totals["stake"] * Decimal("100") if totals["stake"] > 0 else None
    total_live = totals["realized"] + totals["unrealized"]
    total_units = totals["realized_units"] + totals["live_units"]

    return {
        "name": "SH01",
        "bets": totals["bets"],
        "open": totals["open"],
        "wins": totals["wins"],
        "losses": totals["losses"],
        "pushes": totals["pushes"],
        "graded": totals["wins"] + totals["losses"] + totals["pushes"],
        "win_pct": str(win.quantize(Decimal("0.1"))) if win is not None else None,
        "stake_usdc": str(totals["stake"].quantize(Decimal("0.01"))),
        "realized_pnl_usdc": str(totals["realized"].quantize(Decimal("0.01"))),
        "realized_pnl_7d_usdc": str(totals["realized_7d"].quantize(Decimal("0.01"))),
        "realized_pnl_30d_usdc": str(totals["realized_30d"].quantize(Decimal("0.01"))),
        "unrealized_pnl_usdc": str(totals["unrealized"].quantize(Decimal("0.01"))),
        "total_live_pnl_usdc": str(total_live.quantize(Decimal("0.01"))),
        "open_value_usdc": str(totals["open_value"].quantize(Decimal("0.01"))),
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
        "sports": sorted(set(sport_names)),
        "cappers": [],
        "units_called": str(totals["units_called"].quantize(Decimal("0.01"))),
        "realized_units_pnl": str(totals["realized_units"].quantize(Decimal("0.01"))),
        "live_units_pnl": str(totals["live_units"].quantize(Decimal("0.01"))),
        "total_live_units_pnl": str(total_units.quantize(Decimal("0.01"))),
        "missed_units_pnl": "0.00",
        "missed_units_called": "0.00",
        "missed_graded": 0,
        "bet_types": [_summarize_type(type_rows[name]) for name in _BET_TYPES],
        "sport_stats": sport_stats,
        "stats_basis": "visible_verified_sh01_positions",
    }


def _more_stats_with_sh01_truth(mode: str = "live") -> dict[str, Any]:
    payload = _ORIGINAL_MORE_STATS(mode)
    if str(payload.get("mode") or mode or "live").lower() == "paper":
        return payload

    sh01_row = _visible_sh01_summary()
    rows = payload.setdefault("by_capper", [])
    rows[:] = [row for row in rows if str(row.get("name") or "").upper() != "SH01"]
    rows.insert(0, sh01_row)
    payload["sh01"] = sh01_row
    payload["sh01_stats_basis"] = "visible_verified_sh01_positions"
    return payload


metrics._more_stats_payload = _more_stats_with_sh01_truth
