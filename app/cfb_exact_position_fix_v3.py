from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import uvicorn
from polymarket import PublicClient

from app import cfb_exact_position_fix_v2 as base

composite = base.composite
cfb = base.cfb
app = base.app


def _format_signed(value: Decimal) -> str:
    raw = format(value.normalize(), "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return ("+" + raw) if value > 0 else raw


def _owned_token_effective_spread(rec: dict[str, Any]) -> str | None:
    quote = rec.get("quote") or {}
    if str(quote.get("market_type") or rec.get("market_type") or "").lower().strip() != "spread":
        return None
    asset_id = str(quote.get("asset_id") or "").strip()
    outcome_label = str(quote.get("resolved_outcome") or quote.get("requested_outcome") or "").strip()
    if not asset_id or not outcome_label:
        return None

    try:
        with PublicClient() as client:
            markets = list(client.list_markets(clob_token_ids=[asset_id], page_size=10).iter_items())
    except Exception as exc:
        print(
            f"CFB_EXACT_POSITION_LOOKUP_ERROR trade={rec.get('id')} error={type(exc).__name__}: {exc}",
            flush=True,
        )
        return None

    for market in markets:
        try:
            resolved = cfb._spread_outcome_any_line(
                market,
                {
                    "team_hint": outcome_label,
                    "event_hints": [outcome_label],
                    "selection": outcome_label,
                },
            )
        except Exception:
            resolved = None
        if resolved is None:
            continue
        _, outcome, effective_line = resolved
        if str(getattr(outcome, "token_id", "") or "") != asset_id:
            continue
        try:
            return _format_signed(Decimal(str(effective_line)))
        except Exception:
            return None
    return None


def _repair_owned_cfb_spreads() -> None:
    executions = composite.core._load(composite.core.EXECUTIONS_FILE)
    if not isinstance(executions, dict):
        return
    changed = False
    checked: list[dict[str, Any]] = []
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not base.base._is_cfb_execution(rec):
            continue
        quote = rec.get("quote") or {}
        if str(quote.get("market_type") or rec.get("market_type") or "").lower().strip() != "spread":
            continue
        effective = _owned_token_effective_spread(rec)
        checked.append(
            {
                "trade": rec.get("id") or key,
                "asset_id": quote.get("asset_id"),
                "outcome": quote.get("resolved_outcome") or quote.get("requested_outcome"),
                "market": quote.get("market"),
                "old": rec.get("strategy_executed_spread_line"),
                "resolved": effective,
            }
        )
        if not effective:
            continue
        if str(rec.get("strategy_executed_spread_line") or "") != effective:
            rec["strategy_executed_spread_line"] = effective
            executions[key] = rec
            changed = True
    if changed:
        composite.core._save(composite.core.EXECUTIONS_FILE, executions)
    if checked:
        print(f"CFB_OWNED_SPREAD_AUDIT {checked}", flush=True)


_repair_owned_cfb_spreads()


# The v2 wrapper already corrects presentation from market semantics. Replace
# only CFB spread exact_position again from the owned token when available, so
# the displayed line and persisted audit field have the same authority.
_ORIGINAL_ESTIMATE = composite.dashboard._estimate_pnl


def _estimate_pnl_from_owned_token(records: list[dict[str, Any]]):
    rows, total = _ORIGINAL_ESTIMATE(records)
    by_id = {
        str(rec.get("id") or ""): rec
        for rec in records
        if isinstance(rec, dict) and rec.get("id")
    }
    for item in rows:
        if not isinstance(item, dict):
            continue
        rec = by_id.get(str(item.get("id") or ""))
        if not isinstance(rec, dict) or not base.base._is_cfb_execution(rec):
            continue
        quote = rec.get("quote") or {}
        if str(quote.get("market_type") or rec.get("market_type") or "").lower().strip() != "spread":
            continue
        effective = str(rec.get("strategy_executed_spread_line") or "").strip()
        if not effective:
            effective = _owned_token_effective_spread(rec) or ""
        outcome = str(item.get("outcome") or quote.get("resolved_outcome") or quote.get("requested_outcome") or "").strip()
        if effective and outcome:
            item["exact_position"] = f"{outcome} {effective}"
    return rows, total


composite.dashboard._estimate_pnl = _estimate_pnl_from_owned_token


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
