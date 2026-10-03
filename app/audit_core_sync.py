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

    # Audit Core only needs strategy/capper/monitor executions. Ignore unrelated
    # manual wallet activity that has no attributable strategy identity.
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
    }
    sent: dict[str, str] = {}

    async def _sync_once(client: httpx.AsyncClient) -> None:
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
