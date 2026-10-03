from __future__ import annotations

from decimal import Decimal
from typing import Any

from app import sh01_attribution_reconcile_guard_v4 as v4

_EPS = Decimal("0.0001")

_ORIGINAL_PREPROCESS = v4._preprocess_legacy_records
_ORIGINAL_CLEANUP = v4._cleanup_historical_synthetics


def _preprocess_v5(executions: dict[str, Any]) -> int:
    """Normalize legacy synthetic total rows before universal identity repair."""
    changed = _ORIGINAL_PREPROCESS(executions)
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not v4._is_synthetic(rec):
            continue
        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        market = str(quote.get("market") or "")
        low = market.casefold()
        if ("o/u" in low or "over/under" in low) and str(quote.get("market_type") or "").casefold() != "total":
            quote["market_type"] = "total"
            rec["quote"] = quote
            executions[key] = rec
            changed += 1
    return changed


def _cleanup_v5(
    executions: dict[str, Any],
    known: dict[str, Decimal],
    stamp: str,
) -> int:
    """Also correct cost/P&L for historical residual rows whose share count was already right.

    The v4 cleanup reduced oversized synthetic rows, but an already-reduced row could
    still retain the cost basis of the *full* wallet position. Reprice every partial
    residual against its own residual shares when a reliable average entry price is
    present.
    """
    changed = _ORIGINAL_CLEANUP(executions, known, stamp)
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not v4._is_synthetic(rec) or rec.get("stats_excluded"):
            continue
        asset = v4._asset_id(rec)
        snapshot = v4._d(rec.get("wallet_position_size"))
        tracked = known.get(asset, Decimal("0"))
        if not asset or snapshot <= _EPS or tracked <= _EPS:
            continue
        expected = max(Decimal("0"), snapshot - tracked)
        if expected <= _EPS:
            continue

        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        avg = v4._d(quote.get("entry_price") or quote.get("limit_price"))
        current_shares = v4._d(rec.get("filled_shares"))
        current_cost = v4._d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
        desired_cost = expected * avg if avg > 0 else Decimal("0")

        share_wrong = abs(current_shares - expected) > _EPS
        cost_wrong = desired_cost > 0 and abs(current_cost - desired_cost) > Decimal("0.01")
        if share_wrong or cost_wrong:
            v4._reprice_synthetic(rec, expected)
            rec["wallet_reconciled_at"] = stamp
            rec["reconciliation_note"] = (
                "SH01 residual position repriced to residual shares only after subtracting "
                "shares already explained by tracked executions."
            )
            executions[key] = rec
            changed += 1
    return changed


# v4 repair/reconcile functions resolve these module globals at call time, so this
# tight patch upgrades both startup repair and the recurring wallet watcher.
v4._preprocess_legacy_records = _preprocess_v5
v4._cleanup_historical_synthetics = _cleanup_v5
