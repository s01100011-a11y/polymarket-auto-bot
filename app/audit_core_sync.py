from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx

_INSTALLED = False


def _payload(execution_id: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    sport = str(rec.get("strategy_sport") or rec.get("sport") or "").strip().upper()
    source = str(
        rec.get("strategy_source")
        or rec.get("strategy_telegram_source")
        or rec.get("source")
        or ""
    ).strip()
    selection = str(rec.get("strategy_selection") or rec.get("selection") or "").strip()
    if not sport and not source and not selection:
        return None

    quote = rec.get("quote") or {}
    settlement = rec.get("settlement") or {}
    exact_position = (
        rec.get("strategy_exact_position")
        or rec.get("exact_position")
        or quote.get("resolved_outcome")
        or quote.get("requested_outcome")
        or quote.get("market")
    )

    return {
        "id": str(rec.get("id") or execution_id),
        "trade_id": str(rec.get("id") or execution_id),
        "signal_id": rec.get("strategy_pick_id") or rec.get("strategy_signal_id"),
        "sport": sport,
        "source": source,
        "telegram_source": rec.get("strategy_telegram_source"),
        "selection": selection,
        "exact_position": exact_position,
        "status": rec.get("status"),
        "result": settlement.get("result"),
        "stake_usdc": rec.get("actual_cost_usdc") or rec.get("budget_usdc"),
        "shares": rec.get("filled_shares") or rec.get("shares"),
        "entry_price": rec.get("filled_price") or rec.get("avg_fill_price") or quote.get("price"),
        "pnl_usdc": rec.get("realized_pnl"),
        "market_url": rec.get("market_url") or quote.get("market_url"),
        "created_at": rec.get("created_at") or rec.get("queued_at"),
        "updated_at": rec.get("updated_at") or rec.get("closed_at") or rec.get("settled_at"),
    }


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _pw_signal_payload(event_id: str, alert: dict[str, Any]) -> dict[str, Any] | None:
    record = alert.get("pw_export_record")
    if not isinstance(record, dict):
        return None
    meta = alert.get("pw_export_meta") if isinstance(alert.get("pw_export_meta"), dict) else {}
    parsed = alert.get("parsed") if isinstance(alert.get("parsed"), dict) else {}
    sport = str(
        alert.get("pw_export_sport")
        or meta.get("sport")
        or record.get("sport")
        or record.get("league")
        or ""
    ).strip().upper()
    if sport not in {"WNBA", "NBA"}:
        return None

    predicted = _first(
        meta,
        "predicted_winner",
    ) or _first(
        record,
        "predicted_winner",
        "predictedWinner",
        "predicted_team",
        "predictedTeam",
        "pick",
        "selection",
        "team",
    ) or _first(parsed, "predicted_winner", "team", "selection")
    if predicted in (None, ""):
        return None

    posted_at = (
        _first(meta, "event_ts")
        or _first(record, "event_ts", "timestamp", "created_at", "alert_ts", "alert_time", "time", "ts")
        or alert.get("received_at")
    )
    bk_ml = _first(record, "bk_ml", "bkML", "bk_moneyline", "bkMoneyline", "bk_odds", "moneyline", "ml")
    bk_spread = _first(record, "bk_spread", "bkSpread", "live_spread", "spread")
    bk_spread_price = _first(record, "bk_spread_price", "bkSpreadPrice", "spread_price")
    bk_total = _first(record, "bk_total", "bkTotal", "live_total", "liveTotal", "total")
    live_total = _first(record, "live_total", "liveTotal", "bk_total", "bkTotal")
    pregame_total = _first(record, "pregame_total", "pregameTotal", "pre_game_total", "closing_total")
    bk_total_price = _first(record, "bk_total_price", "bkTotalPrice", "total_price")

    available_markets = []
    if bk_ml not in (None, ""):
        available_markets.append("BK ML")
    if bk_spread not in (None, ""):
        available_markets.append("BK SPR")
    if any(value not in (None, "") for value in (bk_total, live_total, pregame_total)):
        available_markets.append("BK TOTAL")

    return {
        "signal_id": str(event_id),
        "source_group": "PW Monitor",
        "source": f"{sport} Monitor",
        "sport": sport,
        "posted_at": posted_at,
        "selection": str(predicted),
        "status": "active",
        "bet_types": ["PW"],
        "available_markets": available_markets,
        "ingest_status": alert.get("status"),
        "game_id": _first(meta, "game_id") or _first(record, "game_id", "gameId", "espn_game_id"),
        "quarter": _first(meta, "quarter") or _first(record, "quarter", "period", "q"),
        "win_probability": _first(meta, "win_probability") or _first(record, "win_probability", "win_prob", "probability", "confidence"),
        "score_at_alert": _first(meta, "score_at_alert") or _first(record, "score_at_alert", "scoreAtAlert", "score"),
        "bk_ml": bk_ml,
        "bk_spread": bk_spread,
        "bk_spread_price": bk_spread_price,
        "bk_total": bk_total,
        "live_total": live_total,
        "pregame_total": pregame_total,
        "bk_total_price": bk_total_price,
        "bk_source": _first(record, "bk_source", "bk_spread_source", "odds_source", "bookmaker"),
        "bk_ts": _first(record, "bk_ts", "odds_ts", "quote_ts"),
        "consensus": _first(record, "consensus", "consensus_score", "consensus_pct"),
        "edge_pp": _first(record, "edge_pp", "edge", "edge_pct"),
        "basis": record.get("basis"),
        "pw_version": record.get("pw_version"),
        "monitor_source": alert.get("monitor_source") or f"{sport} Monitor",
    }


def _signature(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def install(*, app: Any, core: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    base_url = os.getenv("AUDIT_CORE_URL", "").strip().rstrip("/")
    token = os.getenv("AUDIT_CORE_TOKEN", "").strip()
    poll_seconds = max(5, int(os.getenv("AUDIT_CORE_EXECUTION_SYNC_SECONDS", "10")))

    state: dict[str, Any] = {
        "enabled": bool(base_url and token),
        "last_success_at": None,
        "last_error": None,
        "posted": 0,
        "tracked": 0,
        "signals_posted": 0,
        "signals_tracked": 0,
    }
    sent: dict[str, str] = {}
    sent_signals: dict[str, str] = {}

    async def _sync_executions(client: httpx.AsyncClient) -> None:
        executions = core._load(core.EXECUTIONS_FILE)
        if not isinstance(executions, dict):
            return
        state["tracked"] = len(executions)
        for execution_id, rec in executions.items():
            if not isinstance(rec, dict):
                continue
            payload = _payload(str(execution_id), rec)
            if payload is None:
                continue
            signature = _signature(payload)
            key = str(payload["id"])
            if sent.get(key) == signature:
                continue
            response = await client.post(
                f"{base_url}/api/core/executions",
                headers={"X-Audit-Core-Token": token},
                json=payload,
                timeout=8.0,
            )
            response.raise_for_status()
            sent[key] = signature
            state["posted"] = int(state.get("posted") or 0) + 1

    async def _sync_pw_signals(client: httpx.AsyncClient) -> None:
        alerts_file = core.DATA_DIR / "slack_alerts.json"
        alerts = core._load(alerts_file)
        if not isinstance(alerts, dict):
            return
        candidates = []
        for event_id, alert in alerts.items():
            if not isinstance(alert, dict):
                continue
            payload = _pw_signal_payload(str(event_id), alert)
            if payload is not None:
                candidates.append(payload)
        candidates.sort(key=lambda row: str(row.get("posted_at") or ""), reverse=True)
        candidates = candidates[:1000]
        state["signals_tracked"] = len(candidates)

        for payload in candidates:
            key = str(payload.get("signal_id") or "")
            if not key:
                continue
            signature = _signature(payload)
            if sent_signals.get(key) == signature:
                continue
            response = await client.post(
                f"{base_url}/api/core/signals",
                headers={"X-Audit-Core-Token": token},
                json=payload,
                timeout=8.0,
            )
            response.raise_for_status()
            sent_signals[key] = signature
            state["signals_posted"] = int(state.get("signals_posted") or 0) + 1

    async def _sync_once(client: httpx.AsyncClient) -> None:
        await _sync_executions(client)
        await _sync_pw_signals(client)
        from datetime import datetime, timezone
        state["last_success_at"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None

    async def _loop() -> None:
        if not state["enabled"]:
            state["last_error"] = "AUDIT_CORE_URL/AUDIT_CORE_TOKEN not configured"
            return
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await _sync_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state["last_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(poll_seconds)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _audit_core_lifespan(application):
        async with previous_lifespan(application):
            task = asyncio.create_task(_loop())
            try:
                yield
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _audit_core_lifespan

    @app.get("/api/audit-core-sync/status")
    async def _audit_core_sync_status():
        return dict(state)
