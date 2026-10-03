from __future__ import annotations

from decimal import Decimal
from typing import Any

from app import sh01_attribution_reconcile_guard_v4 as v4

_EPS = Decimal("0.0001")

_ORIGINAL_PREPROCESS = v4._preprocess_legacy_records
_ORIGINAL_CLEANUP = v4._cleanup_historical_synthetics


def _preprocess_v5(executions: dict[str, Any]) -> int:
    """Normalize legacy synthetic rows and keep account observations out of performance stats.

    Polymarket account reconciliation creates synthetic SH01 rows so the dashboard can
    still show wallet positions that were not created by a tracked bot execution. Those
    rows are observations of the wallet, not proof that SH01 placed a bet, so they must
    not change SH01 bet count, W-L-P, ROI, realized P/L, or unit P/L.
    """
    changed = _ORIGINAL_PREPROCESS(executions)
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not v4._is_synthetic(rec):
            continue

        if rec.get("performance_excluded") is not True:
            rec["performance_excluded"] = True
            changed += 1
        if rec.get("performance_excluded_reason") != "account_reconcile_observation_not_tracked_execution":
            rec["performance_excluded_reason"] = "account_reconcile_observation_not_tracked_execution"
            changed += 1

        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        market = str(quote.get("market") or "")
        low = market.casefold()
        if ("o/u" in low or "over/under" in low) and str(quote.get("market_type") or "").casefold() != "total":
            quote["market_type"] = "total"
            rec["quote"] = quote
            changed += 1

        executions[key] = rec
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
    present. These rows remain visible as account observations but stay excluded from
    performance statistics.
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


# Keep synthetic account observations visible in SH01 position/history displays, but
# exclude them from every performance aggregation. A synthetic account row can prove
# that shares existed in the wallet; it cannot prove SH01 originated the wager.
_ORIGINAL_STATS = v4.nfl._stats_from_executions
_ORIGINAL_UNIT_SUMMARY = v4.attribution.unit_summary
_ORIGINAL_LEGACY_IDENTITY = v4.metrics._legacy_monitor_identity


def _performance_execs(executions: dict[str, Any]) -> dict[str, Any]:
    return {
        key: rec
        for key, rec in executions.items()
        if not (isinstance(rec, dict) and rec.get("performance_excluded"))
    }


def _stats_without_account_observations(executions: dict[str, Any], *args: Any, **kwargs: Any):
    return _ORIGINAL_STATS(_performance_execs(executions), *args, **kwargs)


def _unit_summary_without_account_observations(
    records: list[dict[str, Any]],
    marks: dict[str, dict[str, Any]],
    **kwargs: Any,
):
    clean = [
        rec for rec in records
        if not (isinstance(rec, dict) and rec.get("performance_excluded"))
    ]
    return _ORIGINAL_UNIT_SUMMARY(clean, marks, **kwargs)


def _identity_without_account_observations(rec: dict[str, Any]):
    if isinstance(rec, dict) and rec.get("performance_excluded"):
        return None, None
    return _ORIGINAL_LEGACY_IDENTITY(rec)


v4.nfl._stats_from_executions = _stats_without_account_observations
v4.attribution.unit_summary = _unit_summary_without_account_observations
v4.metrics._legacy_monitor_identity = _identity_without_account_observations
