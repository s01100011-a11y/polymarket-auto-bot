from __future__ import annotations

import os
from typing import Any

import uvicorn

# Import the normal production composite application first. This preserves the
# exact route/install chain used by /app/start.sh (NFL, CFB, basketball, stats,
# research, dashboard, executor, etc.). FastAPI lifespan/background workers do
# not start until uvicorn begins serving, so the CFB resolver can still be
# patched safely before the first poll.
from app import wnba_pw_research_v13 as composite
from app import cfb_capper_preview as cfb


# Telegram commonly uses PITT while Polymarket indexes the school as Pittsburgh.
# Canonicalizing the query fixes both exact-market lookup and live alternatives.
cfb._CFB_CANONICAL_HINTS["pitt"] = "Pittsburgh"

_ORIGINAL_FIND_SPREAD_ALTERNATIVES = cfb._find_spread_alternatives


def _find_five_better_live_spread_options(
    pick: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to five improved live spread choices for a CFB side pick.

    Pregame behavior is preserved except that the UI can show up to five
    alternatives. Once the game is LIVE, worse-than-capper lines are omitted;
    the dashboard receives only currently tradable BETTER lines, nearest first.
    Each returned row already contains the live quote/odds and alternative_id
    consumed by the existing manual-buy-alternate endpoint/button.
    """
    # Ask the existing resolver for a wide set before filtering. This preserves
    # its event/date/type fail-closed checks and current CLOB quote validation.
    alternatives = _ORIGINAL_FIND_SPREAD_ALTERNATIVES(pick, limit=50)
    if not alternatives:
        return []

    live_rows = [
        row
        for row in alternatives
        if str(row.get("event_phase") or "").upper() == "LIVE"
    ]
    if live_rows:
        better = [
            row
            for row in live_rows
            if str(row.get("relative_to_original") or "").upper() == "BETTER"
        ]
        return better[:5]

    # Pregame: keep established nearest-line ordering, just expose up to five.
    return alternatives[: max(1, min(int(limit or 5), 5))]


cfb._find_spread_alternatives = _find_five_better_live_spread_options


if __name__ == "__main__":
    uvicorn.run(
        composite.app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
