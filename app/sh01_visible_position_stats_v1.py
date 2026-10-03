from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import dashboard_sh01_capper_v3 as sh01

_ORIGINAL_PAYLOAD = sh01._sh01_cappers_payload
_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _position_stats(positions: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    bets = open_count = wins = losses = pushes = 0
    graded_stake = realized = realized_7d = realized_30d = Decimal("0")
    unrealized = open_value = Decimal("0")
    realized_units = live_units = units_called = Decimal("0")

    for item in positions:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        result = str(item.get("result") or "").upper()
        is_open = bool(item.get("sell_available")) or status in _ACTIVE
        is_graded = result in {"WIN", "LOSS", "PUSH"}

        # SH01 is the manual/dashboard/account lane. Its KPI card must describe
        # the same positions that are actually displayed underneath it. Unknown
        # CLOSED_RECONCILED observations are not counted as completed bets until
        # they have an explicit result.
        if not is_open and not is_graded:
            continue

        bets += 1
        stake = _d(item.get("stake_usdc"))
        unit_usdc = _d(item.get("unit_usdc"))
        units = _d(item.get("units"))
        if units > 0:
            units_called += units

        if is_open:
            open_count += 1
            live = _d(item.get("live_pnl_usdc"))
            unrealized += live
            open_value += _d(item.get("current_value_usdc"))
            if unit_usdc > 0:
                live_units += live / unit_usdc
            continue

        pnl = _d(item.get("realized_pnl_usdc"))
        realized += pnl
        graded_stake += stake
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        else:
            pushes += 1
        if unit_usdc > 0:
            realized_units += pnl / unit_usdc

        closed_at = _parse_time(item.get("closed_at") or item.get("submitted_at"))
        if closed_at is not None:
            if closed_at >= cutoff_30d:
                realized_30d += pnl
            if closed_at >= cutoff_7d:
                realized_7d += pnl

    decided = wins + losses
    win_pct = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
    roi = (realized / graded_stake * Decimal("100")) if graded_stake > 0 else None
    total_live = realized + unrealized
    total_units = realized_units + live_units

    return {
        "bets": bets,
        "open": open_count,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "graded": wins + losses + pushes,
        "win_pct": str(win_pct.quantize(Decimal("0.1"))) if win_pct is not None else None,
        "graded_stake_usdc": str(graded_stake.quantize(Decimal("0.01"))),
        "stake_usdc": str(graded_stake.quantize(Decimal("0.01"))),
        "realized_pnl_usdc": str(realized.quantize(Decimal("0.01"))),
        "realized_pnl_7d_usdc": str(realized_7d.quantize(Decimal("0.01"))),
        "realized_pnl_30d_usdc": str(realized_30d.quantize(Decimal("0.01"))),
        "unrealized_pnl_usdc": str(unrealized.quantize(Decimal("0.01"))),
        "total_live_pnl_usdc": str(total_live.quantize(Decimal("0.01"))),
        "open_value_usdc": str(open_value.quantize(Decimal("0.01"))),
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
        "units_called": str(units_called.quantize(Decimal("0.01"))),
        "realized_units_pnl": str(realized_units.quantize(Decimal("0.01"))),
        "live_units_pnl": str(live_units.quantize(Decimal("0.01"))),
        "total_live_units_pnl": str(total_units.quantize(Decimal("0.01"))),
    }


def _payload_with_visible_position_stats() -> dict[str, Any]:
    payload = _ORIGINAL_PAYLOAD()
    sports = payload.get("sports") if isinstance(payload, dict) else None
    if not isinstance(sports, dict):
        return payload

    for row in sports.values():
        if not isinstance(row, dict):
            continue
        positions = row.get("positions")
        if not isinstance(positions, list):
            positions = []
        row.update(_position_stats(positions))
        row["stats_basis"] = "visible_sh01_positions"
    return payload


sh01._sh01_cappers_payload = _payload_with_visible_position_stats
