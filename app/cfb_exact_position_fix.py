from __future__ import annotations

import os
import re
from typing import Any

import uvicorn

from app import cfb_pending_approval_ui as base

composite = base.composite
cfb = base.cfb
app = base.app
remote = cfb.live_control.remote


_SPREAD_RE = re.compile(r"([+-]\d+(?:\.\d+)?)")
_TOTAL_RE = re.compile(r"\b(OVER|UNDER)\s*(\d+(?:\.\d+)?)\b", re.I)


def _is_cfb_execution(rec: dict[str, Any]) -> bool:
    return bool(
        str(rec.get("strategy_sport") or "").upper() == "CFB"
        or str(rec.get("id") or "").startswith("cfb-manual-")
        or str(rec.get("trade_id") or "").startswith("cfb-manual-")
    )


def _signal_for_execution(rec: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any] | None:
    signal_id = str(rec.get("strategy_pick_id") or "").strip()
    if signal_id and isinstance(signals.get(signal_id), dict):
        return signals[signal_id]

    trade_id = str(rec.get("id") or rec.get("trade_id") or "")
    match = re.match(r"cfb-manual-([0-9a-f]{12})-", trade_id, re.I)
    if not match:
        return None
    prefix = match.group(1).lower()
    candidates = [
        row
        for key, row in signals.items()
        if str(key).lower().startswith(prefix) and isinstance(row, dict)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _alternate_line_from_signal(signal: dict[str, Any] | None) -> str | None:
    if not isinstance(signal, dict):
        return None
    direct = str(signal.get("manual_buy_alternate_line") or "").strip()
    if direct:
        return direct

    alternative_id = str(signal.get("manual_buy_alternative_id") or "").strip()
    if not alternative_id:
        return None
    for alt in signal.get("live_alternatives") or []:
        if not isinstance(alt, dict) or str(alt.get("alternative_id") or "") != alternative_id:
            continue
        if alt.get("spread_line") not in {None, ""}:
            return str(alt.get("spread_line"))
        if alt.get("total_line") not in {None, ""}:
            side = str(alt.get("total_side") or "").upper().strip()
            return f"{side} {alt.get('total_line')}".strip()
    return None


def _apply_execution_line(rec: dict[str, Any], alt_line: str | None) -> bool:
    if not alt_line:
        return False
    quote = rec.get("quote") or {}
    kind = str(quote.get("market_type") or rec.get("market_type") or "").lower().strip()
    changed = False

    if rec.get("strategy_alternate_line") != alt_line:
        rec["strategy_alternate_line"] = alt_line
        changed = True

    if kind == "spread":
        match = _SPREAD_RE.search(alt_line)
        if match and rec.get("strategy_executed_spread_line") != match.group(1):
            rec["strategy_executed_spread_line"] = match.group(1)
            changed = True
    elif kind == "total":
        match = _TOTAL_RE.search(alt_line)
        if match and str(rec.get("strategy_executed_total_line") or "") != match.group(2):
            rec["strategy_executed_total_line"] = match.group(2)
            changed = True
    return changed


def _backfill_existing_cfb_execution_lines() -> None:
    signals = composite.core._load(composite.core.DATA_DIR / "cfb_capper_preview_signals.json")
    executions = composite.core._load(composite.core.EXECUTIONS_FILE)
    if not isinstance(signals, dict) or not isinstance(executions, dict):
        return

    changed = False
    repaired: list[dict[str, Any]] = []
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not _is_cfb_execution(rec):
            continue
        signal = _signal_for_execution(rec, signals)
        alt_line = _alternate_line_from_signal(signal)
        if _apply_execution_line(rec, alt_line):
            executions[key] = rec
            changed = True
            repaired.append(
                {
                    "trade": rec.get("id") or key,
                    "pick_id": rec.get("strategy_pick_id"),
                    "market_type": (rec.get("quote") or {}).get("market_type"),
                    "alternate_line": alt_line,
                    "executed_spread": rec.get("strategy_executed_spread_line"),
                    "executed_total": rec.get("strategy_executed_total_line"),
                }
            )

    if changed:
        composite.core._save(composite.core.EXECUTIONS_FILE, executions)
    if repaired:
        print(f"CFB_EXACT_POSITION_BACKFILL {repaired}", flush=True)


# Future manual CFB alternates must carry their selected line into the executor
# record.  This runs before _enqueue writes the request, so the exact-position
# metadata cannot race the phone executor.
if not getattr(remote, "_cfb_exact_position_enqueue_patch", False):
    _ORIGINAL_ENQUEUE = remote._enqueue

    def _enqueue_with_cfb_execution_line(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload or {})
        if str(action).upper() == "BUY" and str(enriched.get("strategy_sport") or "").upper() == "CFB":
            alt_line = str(enriched.get("strategy_alternate_line") or "").strip()
            kind = str(enriched.get("market_type") or "").lower().strip()
            if alt_line and kind == "spread" and enriched.get("strategy_executed_spread_line") in {None, ""}:
                match = _SPREAD_RE.search(alt_line)
                if match:
                    enriched["strategy_executed_spread_line"] = match.group(1)
            elif alt_line and kind == "total" and enriched.get("strategy_executed_total_line") in {None, ""}:
                match = _TOTAL_RE.search(alt_line)
                if match:
                    enriched["strategy_executed_total_line"] = match.group(2)
        return _ORIGINAL_ENQUEUE(action, enriched)

    remote._enqueue = _enqueue_with_cfb_execution_line
    remote._cfb_exact_position_enqueue_patch = True


_backfill_existing_cfb_execution_lines()


# Extra display guard: even if an older execution record is missing the new
# audit fields, use the user-selected CFB alternate saved on its signal rather
# than parsing the opposite side of a Polymarket spread question.
_ORIGINAL_ESTIMATE_PNL = composite.dashboard._estimate_pnl


def _estimate_pnl_with_exact_cfb_lines(records: list[dict[str, Any]]):
    rows, total = _ORIGINAL_ESTIMATE_PNL(records)
    try:
        signals = composite.core._load(composite.core.DATA_DIR / "cfb_capper_preview_signals.json")
    except Exception:
        signals = {}
    if not isinstance(signals, dict):
        signals = {}

    by_id = {
        str(rec.get("id") or ""): rec
        for rec in records
        if isinstance(rec, dict) and rec.get("id")
    }
    for item in rows:
        if not isinstance(item, dict):
            continue
        rec = by_id.get(str(item.get("id") or ""))
        if not isinstance(rec, dict) or not _is_cfb_execution(rec):
            continue
        signal = _signal_for_execution(rec, signals)
        alt_line = _alternate_line_from_signal(signal) or str(rec.get("strategy_alternate_line") or "").strip()
        if not alt_line:
            continue
        quote = rec.get("quote") or {}
        kind = str(quote.get("market_type") or rec.get("market_type") or "").lower().strip()
        if kind == "spread":
            match = _SPREAD_RE.search(alt_line)
            if match:
                outcome = str(item.get("outcome") or quote.get("resolved_outcome") or quote.get("requested_outcome") or "").strip()
                item["exact_position"] = f"{outcome} {match.group(1)}".strip()
        elif kind == "total":
            match = _TOTAL_RE.search(alt_line)
            if match:
                item["exact_position"] = f"{match.group(1).title()} {match.group(2)}"
    return rows, total


composite.dashboard._estimate_pnl = _estimate_pnl_with_exact_cfb_lines


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
