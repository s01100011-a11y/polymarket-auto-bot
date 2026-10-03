from __future__ import annotations

from decimal import Decimal, ROUND_UP
from typing import Any

from app import nfl_capper_ingest as nfl
from app import termux_executor_dashboard as remote


def _hybrid_stake_for_unit_amount(unit_amount_usdc: Any, price: Any) -> Decimal:
    """Stake rule: underdogs risk the full unit; favorites are sized to win the unit."""
    unit_amount = Decimal(str(unit_amount_usdc))
    p = Decimal(str(price))
    if unit_amount <= 0:
        raise ValueError("unit amount must be positive")
    if p <= 0 or p >= 1:
        raise ValueError(f"price must be between 0 and 1, got {p}")

    # Binary price < 0.50 == decimal odds > 2.00 / plus money.
    # For plus-money outcomes, the posted units are the amount RISKED.
    if p < Decimal("0.50"):
        return unit_amount.quantize(Decimal("0.01"), rounding=ROUND_UP)

    # At even money the two conventions are identical. For favorites, size the
    # stake so the winning profit equals the posted unit amount.
    return (unit_amount * p / (Decimal("1") - p)).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )


def _hybrid_stake_for_pick(
    pick: dict[str, Any],
    unit_usdc: Decimal = Decimal("10"),
) -> Decimal:
    unit_amount = nfl._target_profit_for_pick(pick, unit_usdc)
    decimal_odds = nfl._decimal_odds_for_pick(pick)
    if decimal_odds <= 1:
        raise ValueError(f"invalid decimal odds {decimal_odds}")

    # Posted odds > 2.00 are plus-money/underdog prices, so risk the full units.
    if decimal_odds > Decimal("2.00"):
        return unit_amount.quantize(Decimal("0.01"), rounding=ROUND_UP)

    # Favorites and even money retain to-win sizing.
    profit_multiple = decimal_odds - Decimal("1")
    return (unit_amount / profit_multiple).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )


def sizing_mode_for_price(price: Any) -> str:
    p = Decimal(str(price))
    return "RISK_UNITS" if p < Decimal("0.50") else "TO_WIN_UNITS"


def potential_profit_usdc(stake_usdc: Any, price: Any) -> Decimal:
    stake = Decimal(str(stake_usdc))
    p = Decimal(str(price))
    if stake <= 0 or p <= 0 or p >= 1:
        return Decimal("0.00")
    return (stake * (Decimal("1") - p) / p).quantize(
        Decimal("0.01"), rounding=ROUND_UP
    )


# Keep the existing public helper names so NFL, CFB, UFC and any other sports
# already using the shared NFL sizing helpers automatically receive the rule.
nfl._stake_to_win_at_price = _hybrid_stake_for_unit_amount
nfl._stake_for_pick = _hybrid_stake_for_pick


# Normalize the metadata written to new queued unit-based orders too. This keeps
# Audit DB/dashboard rows truthful: plus-money bets are RISK_UNITS, favorites
# are TO_WIN_UNITS, and target_profit_usdc reflects the actual binary payout.
_ORIGINAL_ENQUEUE = remote._enqueue


def _enqueue_with_hybrid_unit_metadata(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    if str(action or "").upper() in {"BUY", "PREVIEW"}:
        try:
            p = Decimal(str(data.get("max_price") or data.get("signal_buy_price")))
            units = Decimal(str(data.get("strategy_units") or "0"))
            unit_usdc = Decimal(str(data.get("strategy_unit_usdc") or "0"))
            stake = Decimal(str(data.get("budget_usdc") or "0"))
        except Exception:
            p = units = unit_usdc = stake = Decimal("0")
        if Decimal("0") < p < Decimal("1") and units > 0 and unit_usdc > 0:
            mode = sizing_mode_for_price(p)
            data["strategy_sizing_mode"] = mode
            data["sizing_mode"] = mode
            data["strategy_unit_amount_usdc"] = str(
                (units * unit_usdc).quantize(Decimal("0.01"), rounding=ROUND_UP)
            )
            if stake > 0:
                data["strategy_target_profit_usdc"] = str(potential_profit_usdc(stake, p))
    return _ORIGINAL_ENQUEUE(action, data)


remote._enqueue = _enqueue_with_hybrid_unit_metadata
