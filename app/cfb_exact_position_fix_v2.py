from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any

import uvicorn

from app import cfb_exact_position_fix as base

composite = base.composite
cfb = base.cfb
app = base.app


_QUESTION_SPREAD_RE = re.compile(
    r"(?:^|\bspread\s*:\s*)(?P<team>.+?)\s*\(?(?P<line>[+-]\d+(?:\.\d+)?)\)?\s*$",
    re.I,
)


def _norm_team(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _format_signed(value: Decimal) -> str:
    raw = format(value.normalize(), "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return ("+" + raw) if value > 0 else raw


def _effective_spread_from_quote(rec: dict[str, Any]) -> str | None:
    """Return the spread attached to the purchased outcome, not question team.

    Polymarket encodes a binary spread market from one named team's perspective,
    e.g. ``Spread: Virginia Tech (-6.5)``. If the purchased outcome is the other
    team (Pittsburgh), that outcome is Pittsburgh +6.5.
    """
    quote = rec.get("quote") or {}
    if str(quote.get("market_type") or rec.get("market_type") or "").lower().strip() != "spread":
        return None

    market = str(quote.get("market") or "").strip()
    outcome = str(quote.get("resolved_outcome") or quote.get("requested_outcome") or "").strip()
    if not market or not outcome:
        return None

    match = _QUESTION_SPREAD_RE.search(market)
    if not match:
        # Common Gamma question form may contain extra text before "Spread:".
        match = re.search(
            r"Spread\s*:\s*(?P<team>.+?)\s*\(?(?P<line>[+-]\d+(?:\.\d+)?)\)?(?:\s|$)",
            market,
            re.I,
        )
    if not match:
        return None

    market_team = str(match.group("team") or "").strip(" :-()")
    try:
        question_line = Decimal(str(match.group("line")))
    except Exception:
        return None
    if not market_team or question_line == 0:
        return None

    if _norm_team(market_team) == _norm_team(outcome):
        effective = question_line
    else:
        # This is a binary team spread market. The non-named outcome owns the
        # opposite handicap. Do not use this inference outside spread records.
        effective = -question_line
    return _format_signed(effective)


def _repair_persisted_cfb_spreads() -> None:
    executions = composite.core._load(composite.core.EXECUTIONS_FILE)
    if not isinstance(executions, dict):
        return
    changed = False
    repaired: list[dict[str, Any]] = []
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not base._is_cfb_execution(rec):
            continue
        effective = _effective_spread_from_quote(rec)
        if not effective:
            continue
        if str(rec.get("strategy_executed_spread_line") or "") == effective:
            continue
        old = rec.get("strategy_executed_spread_line")
        rec["strategy_executed_spread_line"] = effective
        executions[key] = rec
        changed = True
        repaired.append(
            {
                "trade": rec.get("id") or key,
                "outcome": (rec.get("quote") or {}).get("resolved_outcome") or (rec.get("quote") or {}).get("requested_outcome"),
                "market": (rec.get("quote") or {}).get("market"),
                "old": old,
                "executed_spread": effective,
            }
        )
    if changed:
        composite.core._save(composite.core.EXECUTIONS_FILE, executions)
    if repaired:
        print(f"CFB_SPREAD_SIGN_REPAIR {repaired}", flush=True)


_repair_persisted_cfb_spreads()


# Final presentation guard. The persisted repair above is authoritative for
# history/settlement; this keeps a stale in-memory snapshot from displaying the
# question team's handicap before the next reload cycle.
_ORIGINAL_ESTIMATE = composite.dashboard._estimate_pnl


def _estimate_pnl_with_outcome_spread(records: list[dict[str, Any]]):
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
        if not isinstance(rec, dict) or not base._is_cfb_execution(rec):
            continue
        effective = _effective_spread_from_quote(rec)
        if not effective:
            continue
        quote = rec.get("quote") or {}
        outcome = str(item.get("outcome") or quote.get("resolved_outcome") or quote.get("requested_outcome") or "").strip()
        if outcome:
            item["exact_position"] = f"{outcome} {effective}"
    return rows, total


composite.dashboard._estimate_pnl = _estimate_pnl_with_outcome_spread


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
