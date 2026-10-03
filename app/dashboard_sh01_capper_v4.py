from __future__ import annotations

import os
from typing import Any

import uvicorn

from app import dashboard_sh01_capper_v3 as base

app = base.app
dashboard = base.dashboard
core = base.core

_ORIGINAL_PAYLOAD = base._sh01_cappers_payload
_GENERIC_OUTCOMES = {"UNDER", "OVER", "YES", "NO"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _enrich_position(item: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """Keep account-reconciled SH01 rows self-describing on every card.

    Synthetic wallet records often have no strategy_selection because there was no
    bot/capper signal.  The normal position formatter then falls back to the bare
    outcome token (for example, ``Under``).  Preserve the actual Polymarket market
    title and execution metadata instead of showing an ambiguous outcome-only row.
    """
    out = dict(item)
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}

    market = _text(quote.get("market") or out.get("market"))
    market_url = _text(quote.get("market_url") or out.get("market_url"))
    selection = _text(out.get("selection") or rec.get("strategy_selection"))
    outcome = _text(out.get("outcome") or quote.get("resolved_outcome") or quote.get("requested_outcome"))

    # A bare binary outcome is not a meaningful card title.  Prefer the exact
    # Polymarket market question/title for synthetic account rows.
    if market and (not selection or selection.upper() in _GENERIC_OUTCOMES):
        selection = market

    if selection:
        out["selection"] = selection
    if market:
        out["market"] = market
    if market_url:
        out["market_url"] = market_url
    if outcome:
        out["outcome"] = outcome

    if out.get("shares") in {None, "", "—"}:
        shares = rec.get("filled_shares")
        if shares in {None, ""}:
            shares = rec.get("remaining_shares")
        if shares in {None, ""}:
            shares = quote.get("shares")
        if shares not in {None, ""}:
            out["shares"] = shares

    if out.get("entry_price") in {None, "", "—"}:
        entry = quote.get("entry_price") or quote.get("limit_price")
        if entry not in {None, ""}:
            out["entry_price"] = entry

    if out.get("stake_usdc") in {None, "", "—"}:
        stake = rec.get("actual_cost_usdc") or rec.get("budget_usdc")
        if stake not in {None, ""}:
            out["stake_usdc"] = stake

    out["account_reconciled"] = str(rec.get("source") or "") == "polymarket_account_reconcile"
    out["attribution_reason"] = rec.get("attribution_reason")
    return out


def _sh01_cappers_payload_v4() -> dict[str, Any]:
    payload = _ORIGINAL_PAYLOAD()
    raw = core._load(core.EXECUTIONS_FILE)
    executions = raw if isinstance(raw, dict) else {}

    for sport, row in (payload.get("sports") or {}).items():
        positions = row.get("positions") if isinstance(row, dict) else None
        if not isinstance(positions, list):
            continue
        enriched: list[dict[str, Any]] = []
        for item in positions:
            if not isinstance(item, dict):
                continue
            trade_id = _text(item.get("trade_id") or item.get("id"))
            rec = executions.get(trade_id)
            if isinstance(rec, dict):
                item = _enrich_position(item, rec)
            enriched.append(item)
        row["positions"] = enriched
    return payload


# Existing FastAPI route in v3 resolves this module-global callable at request
# time, so replacing it upgrades the current endpoint without registering a
# duplicate route.
base._sh01_cappers_payload = _sh01_cappers_payload_v4


def _log_synthetic_market_audit() -> None:
    """One concise, non-secret startup audit to make future orphan rows traceable."""
    raw = core._load(core.EXECUTIONS_FILE)
    executions = raw if isinstance(raw, dict) else {}
    for trade_id, rec in executions.items():
        if not isinstance(rec, dict) or not str(trade_id).startswith("sh01-account-"):
            continue
        if rec.get("stats_excluded"):
            continue
        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        print(
            "SH01_MARKET_AUDIT "
            f"trade={trade_id} sport={rec.get('strategy_sport')!r} "
            f"status={rec.get('status')!r} market={quote.get('market')!r} "
            f"outcome={(quote.get('resolved_outcome') or quote.get('requested_outcome'))!r} "
            f"shares={rec.get('filled_shares')!r} cost={(rec.get('actual_cost_usdc') or rec.get('budget_usdc'))!r}",
            flush=True,
        )


_log_synthetic_market_audit()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
