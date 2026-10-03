from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import uvicorn
from fastapi import Depends, HTTPException

from app import cfb_live_options_bootstrap as live_bootstrap

composite = live_bootstrap.composite
cfb = live_bootstrap.cfb
app = composite.app

TOTAL_ROUTE = "/api/cfb-cappers/manual-buy-total-alternate/{signal_id}/{alternative_id}"

# Replace the first-generation total route with one that surfaces the real
# executor queue state (including MIN_ODDS approval) and refuses duplicate
# clicks for the same exact live total while that request is still active.
app.router.routes[:] = [
    route
    for route in app.router.routes
    if not (
        getattr(route, "path", None) == TOTAL_ROUTE
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


def _active_same_line_request(signal_id: str, exact_line: str) -> dict[str, Any] | None:
    remote = cfb.live_control.remote
    try:
        queue = remote._queue_load()
    except Exception:
        return None
    for request_id, queued in queue.items():
        if not isinstance(queued, dict) or queued.get("action") != "BUY":
            continue
        status = str(queued.get("status") or "").upper()
        if status not in {"PENDING", "LEASED", "WAITING_APPROVAL"}:
            continue
        payload = queued.get("payload") or {}
        if str(payload.get("strategy_pick_id") or "") != signal_id:
            continue
        if str(payload.get("strategy_alternate_line") or "") != exact_line:
            continue
        return {"id": str(request_id), **queued}
    return None


def _queue_state(request_id: str) -> tuple[str, dict[str, Any]]:
    remote = cfb.live_control.remote
    try:
        queued = remote._queue_load().get(str(request_id)) or {}
    except Exception:
        queued = {}
    status = str(queued.get("status") or "PENDING").upper()
    return status, queued if isinstance(queued, dict) else {}


@app.post(TOTAL_ROUTE, dependencies=[Depends(composite.dashboard._auth)])
def cfb_capper_manual_buy_total_alternate_fixed(signal_id: str, alternative_id: str) -> dict[str, Any]:
    signal_file = composite.core.DATA_DIR / "cfb_capper_preview_signals.json"
    signals = composite.core._load(signal_file)
    if not isinstance(signals, dict):
        signals = {}
    record = signals.get(signal_id)
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail="Unknown CFB signal")
    if record.get("source") not in cfb.SOURCE_LABELS:
        raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

    pick = cfb._persisted_record_pick(record)
    if not isinstance(pick, dict):
        raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
    kind, _ = cfb._classify_pick(pick)
    if kind != "total":
        raise HTTPException(status_code=409, detail="This endpoint is only for full-game total signals")

    try:
        alternatives = live_bootstrap._find_five_better_live_total_options(pick, limit=5)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"Could not refresh live total alternatives: {exc}") from exc
    selected = next(
        (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
        None,
    )
    if selected is None:
        raise HTTPException(
            status_code=409,
            detail="That live total is no longer available; refresh the dashboard and choose a current line",
        )
    if cfb._event_phase(selected) == "CLOSED":
        raise HTTPException(status_code=409, detail="Selected Polymarket total market is closed")

    exact_line = f"{selected['total_side']} {selected['total_line']}"
    existing = _active_same_line_request(signal_id, exact_line)
    if existing is not None:
        qstatus = str(existing.get("status") or "PENDING").upper()
        payload = existing.get("payload") or {}
        approval_required = qstatus == "WAITING_APPROVAL"
        record["manual_buy_request_id"] = existing.get("id")
        record["manual_buy_status"] = qstatus
        record["approval_required"] = approval_required
        record["approval_reason"] = payload.get("approval_reason") if approval_required else None
        record["signal_decimal_odds"] = payload.get("signal_decimal_odds")
        record["minimum_decimal_odds"] = payload.get("minimum_decimal_odds")
        record["manual_buy_alternate_line"] = exact_line
        record["manual_buy_alternative_id"] = alternative_id
        record["updated_at"] = cfb._now_iso()
        signals[signal_id] = record
        composite.core._save(signal_file, signals)
        return {
            "ok": True,
            "duplicate": True,
            "signal_id": signal_id,
            "selected_live_line": exact_line,
            "request_id": existing.get("id"),
            "queue_status": qstatus,
            "approval_required": approval_required,
            "approval_reason": payload.get("approval_reason"),
            "signal_decimal_odds": payload.get("signal_decimal_odds"),
            "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
        }

    execution_pick = dict(pick)
    execution_pick["total_side"] = str(selected["total_side"])
    execution_pick["total_line"] = selected["total_line"]
    hints = [str(x) for x in (pick.get("event_hints") or []) if str(x)]
    matchup = "/".join(hints) if len(hints) == 2 else str(pick.get("selection") or "CFB total")
    execution_pick["selection"] = f"{matchup} {exact_line}"

    try:
        effective_unit_usdc = cfb.nfl._capper_unit_usdc(
            composite.core,
            str(record.get("source") or ""),
            Decimal(os.getenv("CFB_CAPPER_UNIT_USDC", "10")),
        )
        result = cfb._prepare_manual_buy(
            execution_pick,
            core=composite.core,
            remote=cfb.live_control.remote,
            unit_usdc=effective_unit_usdc,
            matched=selected,
            strategy_pick_id=signal_id,
            strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
            strategy_alternate_line=exact_line,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    qstatus, queued = _queue_state(str(result["request_id"]))
    payload = queued.get("payload") or {}
    approval_required = qstatus == "WAITING_APPROVAL"
    approval_reason = payload.get("approval_reason") if approval_required else None

    record["manual_buy_request_id"] = result["request_id"]
    record["manual_buy_trade_id"] = result["trade_id"]
    record["manual_buy_status"] = qstatus
    record["manual_buy_error"] = None
    record["manual_buy_at"] = cfb._now_iso()
    record["manual_buy_alternate_line"] = exact_line
    record["manual_buy_alternative_id"] = alternative_id
    record["approval_required"] = approval_required
    record["approval_reason"] = approval_reason
    record["signal_decimal_odds"] = payload.get("signal_decimal_odds")
    record["minimum_decimal_odds"] = payload.get("minimum_decimal_odds")
    record["live_alternatives"] = alternatives
    record["updated_at"] = cfb._now_iso()
    signals[signal_id] = record
    composite.core._save(signal_file, signals)

    print(
        "CFB_TOTAL_MANUAL_BUY "
        + str({
            "signal_id": signal_id,
            "line": exact_line,
            "request_id": result["request_id"],
            "queue_status": qstatus,
            "approval_reason": approval_reason,
            "best_ask": result.get("best_ask"),
        }),
        flush=True,
    )

    return {
        "ok": True,
        "signal_id": signal_id,
        "original_selection": record.get("selection"),
        "selected_live_line": exact_line,
        "request_id": result["request_id"],
        "trade_id": result["trade_id"],
        "queue_status": qstatus,
        "approval_required": approval_required,
        "approval_reason": approval_reason,
        "signal_decimal_odds": payload.get("signal_decimal_odds"),
        "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
        "best_ask": result.get("best_ask"),
        "live_odds_american": result.get("live_odds_american"),
    }


# Make alternate BUY feedback explicit instead of leaving a disabled grey button.
_old = """  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();"""
_new = """  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  const q=String(d.queue_status||'QUEUED').toUpperCase();
  if(d.approval_required||q==='WAITING_APPROVAL'){
   btn.textContent='APPROVAL REQUIRED';
   btn.disabled=false;
  }else{
   btn.textContent=q.replaceAll('_',' ');
  }
  await loadCfbCapperStats();"""
if _old in composite.dashboard.DASHBOARD_HTML:
    composite.dashboard.DASHBOARD_HTML = composite.dashboard.DASHBOARD_HTML.replace(_old, _new, 1)


# Log only non-sensitive state for any already-created total requests so a
# deploy can reconcile prior grey-button clicks without submitting anything.
try:
    queue = cfb.live_control.remote._queue_load()
    for request_id, queued in queue.items():
        if not isinstance(queued, dict) or queued.get("action") != "BUY":
            continue
        payload = queued.get("payload") or {}
        if payload.get("strategy_sport") != "CFB" or payload.get("market_type") != "total":
            continue
        print(
            "CFB_TOTAL_QUEUE_STATE "
            + str({
                "request_id": request_id,
                "status": queued.get("status"),
                "pick_id": payload.get("strategy_pick_id"),
                "line": payload.get("strategy_alternate_line"),
                "approval_reason": payload.get("approval_reason"),
                "signal_decimal_odds": payload.get("signal_decimal_odds"),
                "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
                "trade_id": payload.get("trade_id"),
            }),
            flush=True,
        )
except Exception as exc:
    print(f"CFB_TOTAL_QUEUE_STATE_ERROR {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
